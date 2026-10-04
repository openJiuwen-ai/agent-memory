# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ExtractorImpl — M1 信息提取实现（3 Phase 流水线）。

三阶段流水线（接口契约见 docs/specs/S05-construction.md Extractor 节）：
  Phase 1  预处理 — 过滤 lifecycle≠ACTIVE / 空 content
  Phase 2  LLM 提取 — 批量拼一个 prompt 调 LLM → 解析 JSON → ExtractionCandidate
  Phase 3  构建 MemoryUnit — candidate → MemoryUnit(provenance=[source_id])

Phase 2 采用批量提取策略：全部 unit 拼一个 prompt，每条带 ``[ID: unit_id]`` 标记，
LLM 在每个候选里输出裸 ``source_id`` 回指来源。解析时校验 source_id 存在于本批 unit，
缺失/错配的候选丢弃。``_strip_source_id_shell`` 兜底剥掉 LLM 误带的 ``[ID: ]`` 外壳，
避免 source_id 恒不匹配导致候选全丢。

tier 与 tags 均由 LLM 在抽取时一并产出（同一 prompt，不另调 LLM、不走 FeatureExtractor）：
- ``tier``：候选认知角色，限定 ``episodic`` / ``semantic`` / ``procedural`` 三选一
  （WORKING/ARCHIVAL 不由抽取产出，CORE 留给 Abstractor 升华）。
- ``tags``：1-3 个简短主题标签；构建派生 unit 时与源 unit 的 write tags 合并，再追加
  ``extracted``（或 procedural 路径的 ``procedural``）系统标记。

L0/L1 分层标注不由本算子负责——由 Evolver 在抽取后委托
:class:`~construction.layer_annotator.LayerAnnotator` 生成（见 F01-memory-layer）。

粒度策略：事实边界按「该行提出的一件事」划分，而非按从句划分——一个主张连同说话人为它
给出的理由/属性属于同一件事，不拆成并列条目。该约束是双向的：既不把一件事拆成并列碎片，
也不允许第二件事在合并中消失。只写单向（「不要拆」）会让模型一路收敛到零产出，是漏抽的
直接成因。这不改变抽象层级：跨 source 合并与抽象升华仍由 Abstractor 负责，本算子只产低
抽象粒度的派生单元。跨次 write 的重复由 Evolver ``_dedup_batch`` 兜底（向量召回判定
NOOP/UPDATE）。

时间信息的落点由检索面决定：条目被召回后，读者只看得到 ``content`` 文本，``event_date``
是过滤字段、不进阅读面。因此使条目在时间上可定位的信息必须写进文本；是否需要时间由该条
自身有无时间维度决定，不按事实类别预设。

言语行为：该行若在建议/请求/承诺/拒绝/回答提问，条目须写出该行为及其对象；只保留命题会
丢掉该行的用途。裸转述壳（"A said that ..."）不属于事实本身。社交外壳（寒暄、致谢、赞美、
道别）与信息内容在同一行内并存，判定该行有无内容须在剥离外壳之后。

语境取自 ``ExtractContext.recent_originals``：上文不作提取来源、不改 ``source_id``，但
省略式应答（承接上一行、单独不成命题）允许据上文补全为独立条目。

纯函数：不落盘、不标记、不检索。幂等性依赖 LLM temperature=0。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import (
    get_logger,
    metadata_for_log,
    redact_for_log,
)
from jiuwen_memory.common.type_def import (
    Entity,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Segment,
    Temporal,
    inherited_system_metadata,
    inherited_user_metadata,
)

from ..base import ExtractContext, OperatorType
from ..common import merge_unit_tags, parse_tags
from ..extractor import Extractor, ExtractorProducer

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 内部类型（实现层，不暴露到 __init__.py）
# ---------------------------------------------------------------------------


class InvalidExtractionJSONError(ValueError):
    """LLM 抽取结果不是合法 JSON；与模型明确返回 ``[]`` 区分。"""


class InvalidExtractionCandidateError(ValueError):
    """LLM 抽取候选不满足最小结构契约。"""


class ExtractionTarget(str, Enum):
    """LLM 产出的提取目标类型。"""

    FACT = "fact"
    EVENT = "event"
    PREFERENCE = "preference"
    CONTEXT = "context"
    STRUCTURED_RECORD = "structured_record"
    ARTIFACT = "artifact"


@dataclass
class ExtractionCandidate:
    """LLM 产出的单条候选项。"""

    target: ExtractionTarget = ExtractionTarget.FACT
    content: str = ""
    source_unit_id: str = ""
    source_span: tuple[int, int] = (0, 0)
    confidence: float = 0.0
    evidence: str = ""
    event_date: str = ""  # LLM 解析出的绝对事件时间（ISO 8601），写入 temporal.t_event
    tier: MemoryTier = MemoryTier.SEMANTIC  # LLM 判定的认知角色（非法值兜底 SEMANTIC）
    tags: list[str] = field(default_factory=list)  # LLM 抽的 1-3 个主题标签
    keywords: list[str] = field(default_factory=list)  # 保留字段，普通抽取路径不再填充
    entities: list[Entity] = field(default_factory=list)  # 保留字段，普通抽取路径不再填充
    metadata: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LLM prompt — 批量提取，每条 unit 带 [ID: uuid] 标记，LLM 输出裸 uuid 作 source_id
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM_PROMPT = """\
Output ONLY a JSON array. No explanation, no markdown fences.

For each source line: resolve speaker and pronouns, resolve time against the observation
date, decide what the line does, then extract items.

Rules:

- Extract what the line states, plus what follows from it in one direct step. Never
  speculate.

- Keep every specific: every named entity, quantity and descriptive attribute must
  survive in the source's own wording, never replaced by the category it belongs to.
  GOOD "A's flight to Lisbon was delayed by two hours."  BAD "A's travel was disrupted."

- Granularity follows the information, not the length: one matter per item, and every
  matter the line raises becomes an item. A claim plus the reasons or attributes offered
  for it is ONE matter - one point backed by three attributes yields ONE item, not four.
  Do not split one matter into parallel fragments; do not let a second matter vanish into
  the first. Never generalise, raise abstraction, or combine across source lines.

- Write what the line DOES. Test the verb: if the proposition holds whether or not it was
  uttered, the verb is a reporting wrapper (said, stated, mentioned, noted) - drop it and
  state the fact directly. If the utterance is itself what creates the fact - a
  suggestion, request, promise, refusal, agreement or answer - keep that verb and name
  whom it addresses; an item left with only the proposition loses what the line was for.
  Keep a reporting wrapper only where who said it, or when, is itself the point.
  GOOD "On 2026-03-14, asked by B where to hold the workshop, A proposed the riverside
        centre - walkable from the station, rooms for forty."
  BAD  "A mentioned that the deadline is Friday."  ->  "The deadline is Friday."

- Self-containment: each item stands alone, carrying whatever time, place, party or
  occasion gives the fact its meaning. "A met a former colleague" is incomplete;
  "During A's conference trip to Berlin, A met a former colleague" is not.

- Speaker attribution: a leading speaker marker names who is talking. "I/me/my/we" are
  that speaker, "you/your" whoever is addressed. Write every item in the third person
  with an explicit subject, naming each party as the source labels them, and resolve
  every pronoun. Never assert of the addressee what the speaker only urged, asked or
  assumed: a line marked A reading "I renewed my licence, and you should renew yours"
  yields "A renewed their licence" and "A urged B to renew B's licence" - never "B
  renewed their licence".

- Time. The user message opens with "observation_date: YYYY-MM-DD" - the date the lines
  were said and the reference for every time expression; the lines carry no date.
  (a) Whatever places the item in time belongs in the CONTENT TEXT. A reader of the item
      sees only that text, so a time left in "event_date" alone is invisible and does not
      count. The observation date is that time unless the source gives another. A fact
      that holds without reference to any time needs none: "On 2026-03-14, A moved to the
      Lisbon office." but "A prefers written handovers over verbal ones."
  (b) Resolve every relative expression ("yesterday", "a year ago") against that date and
      REMOVE the original wording - never keep both. This holds for standing facts too:
      "A has preferred written handovers since 2025."
  (c) Never state a precision the source lacks: a named day stays a day, "last month"
      becomes a month, "last week" a range; do not invent an hour. The observation date
      is known at day precision.
  (d) "event_date": ISO 8601, omitted when the item has no time component. At month or
      range precision, put the first day here and keep the content text coarser.

- Language: write each statement in the same language as its source. Never translate.

- A line that answers or continues the one before it may be elliptical on its own.
  Complete it from the recent context into a standalone item; the item is still this
  line's and keeps its source_id.
  GOOD context "C proposes rewriting the parser in Rust; D asks where to start"
       line "[C]: The lexer first."  ->  "C plans to start the rewrite with the lexer."

- Social framing - greeting, thanks, praise, sign-off - is not content, and it also does
  not make the rest of the line disappear. Judge what the line adds once that framing is
  set aside; return [] only when nothing is left.

Fields:
- "source_id": the bare UUID from the "[ID: ...]" marker, no wrapper or quotes - the line
  the item is about. Context may supply a referent or what an elliptical line answers, and
  never changes this id; a matter only the context raises is not an item.
- "confidence": 1.0 stated outright, 0.7 directly entailed, 0.5 weakly suggested. Never
  emit below 0.5.
- "tier": "episodic" happened at a point in time; "semantic" a fact, concept or
  preference; "procedural" how something is done. When unsure, "semantic".
- "tags": 1-3 short lowercase labels for the topic, in the content's language, not
  repeating words already central to the content.

Output schema:
[{
  "source_id": "32049cd0-5f7c-419f-928f-503b24318f7c",
  "target": "fact" | "event" | "preference" | "context" | "structured_record" | "artifact",
  "tier": "episodic" | "semantic" | "procedural",
  "content": "one self-contained statement, every specific kept",
  "tags": ["tag1", "tag2"],
  "confidence": 0.5~1.0,
  "event_date": "2026-03-14"
}]
"""

# Optional, call-scoped extraction guard used by LongMemEval.  The default prompt above
# remains byte-for-byte equivalent to the baseline path; callers must opt in through
# ``metadata["extraction_fidelity_mode"]`` on a current source unit.
_EXTRACTION_FIDELITY_SUFFIX = """\
Additional fidelity contract for this write only:

- Do not merge independent matters from different source lines into one item.
- A normal answer or confirmation becomes the resolved proposition it contributes; do
  not also emit a duplicate "answered that ..." wrapper.
- Complete an informative CURRENT elliptical line from only the immediately relevant
  context, copying the minimum missing subject, predicate, object, unit or time. Never
  emit a fact that belongs only to context.
- Before returning, verify that every durable entity, value, date, negation, update and
  choice contributed by each current source line appears exactly once.
"""

# 过程记忆抽取 prompt：把本轮对话汇总成 1 条结构化执行历史（PROCEDURAL tier）。
# 不抽离散事实、不产多条，只产 1 条自包含的「目标→步骤→结果」式汇总。
_PROCEDURAL_SYSTEM_PROMPT = """\
Summarize the conversation below into ONE structured execution-history memory.

Output ONLY a JSON object (no array, no markdown fences). Schema:
{
  "content": "one self-contained statement summarizing what was done in this turn: \
the goal/task, the key steps taken, and the outcome/result. Structured but as a single \
coherent statement; inline markers such as Goal:/Steps:/Result: are optional, and must be \
written in the source's language when used. Write the whole statement in the SAME \
language as the source text. Cover the whole turn, not just one fact."
}

Rules:
- Produce exactly ONE memory (the whole turn as one procedural record).
- Do NOT fragment into multiple facts; do NOT output a JSON array.
- Capture the procedure/how-to: what the user wanted, what was done, how it ended.
- Keep it self-contained (understandable without the source conversation).
- If the turn has no actionable procedure (e.g. pure greeting), still summarize its intent
  and outcome in one statement.
"""

_SOURCE_PREFIX = """\
---
[ID: {unit_id}]
{unit_content}
---
"""


# 匹配 LLM 误带 [ID: ...] 外壳的 source_id（prompt 已要求裸 uuid，此处为防御性兜底）
_SOURCE_ID_SHELL_RE = re.compile(r"^\s*\[ID:\s*(?P<id>[^\]]+)\]\s*$", re.IGNORECASE)

# 批量提取的子批大小：把 accepted 拆成若干子批，每子批独立一次 LLM 调用。
# 单个子批失败与其它子批隔离；仅当整次抽取没有产生任何可用候选时向上抛错，
# 避免把完全失败伪装成模型明确返回的正常空抽取。
# 默认值 10 与 middle_batch_size 对齐，可通过 extract_batch_size 配置覆盖。
_DEFAULT_EXTRACT_BATCH_SIZE = 10


def _strip_source_id_shell(raw: str) -> str:
    """剥掉 source_id 可能带的 ``[ID: ...]`` 外壳，返回裸 id。

    prompt 已要求 LLM 输出裸 uuid，但部分模型仍会原样回带 ``[ID: ...]`` 标记——
    解析端兜底剥壳，避免 source_id 恒不匹配 unit_map 导致候选全被丢弃。
    """
    m = _SOURCE_ID_SHELL_RE.match(raw)
    return m.group("id").strip() if m else raw.strip()


def _parse_event_date(raw: str) -> datetime | None:
    """解析 LLM 输出的 event_date（ISO 8601）为 datetime；失败返回 None。

    兼容带/不带时区、仅日期（YYYY-MM-DD）等形式。解析失败不阻断——回退到 source.t_event。
    """
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        logger.debug("Extractor._parse_event_date: cannot parse %r, fallback", s)
        return None


# 抽取阶段允许 LLM 产出的 tier（其余值兜底为 SEMANTIC）。
_EXTRACT_ALLOWED_TIERS: set[str] = {
    MemoryTier.EPISODIC.value,
    MemoryTier.SEMANTIC.value,
    MemoryTier.PROCEDURAL.value,
}


def _parse_tier(raw) -> MemoryTier:
    """解析 LLM 输出的 tier，限定 episodic/semantic/procedural；非法/缺失兜底 SEMANTIC。

    prompt 限定三选一，但 LLM 可能返回大写、带空格或越界值（如 core/working）——
    一律归一到三选一，越界值降级 SEMANTIC（宁可保守，不产出 WORKING/ARCHIVAL/CORE）。
    """
    s = str(raw or "").strip().lower()
    if s in _EXTRACT_ALLOWED_TIERS:
        return MemoryTier(s)
    if s:
        logger.debug("Extractor._parse_tier: unknown tier %r, fallback SEMANTIC", s)
    return MemoryTier.SEMANTIC


def _format_context_block(context: ExtractContext | None) -> str:
    """把 ExtractContext 拼成 user prompt 末尾的参考段（无 context 返回空串）。

    - Recent conversation context：做指代/代词消解与语境，明示「不从中提取、
      source_id 不指向这些」。
    - Existing related memories：已有事实，明示「不要重复产出已涵盖的信息」。
    """
    if context is None:
        return ""
    parts: list[str] = []
    if context.recent_originals:
        lines = [f"- {u.content}" for u in context.recent_originals if u.content]
        if lines:
            parts.append(
                "## Recent conversation context "
                "(for coreference / pronoun resolution only — do NOT extract from these, "
                "do NOT point source_id at these):\n"
                + "\n".join(lines)
            )
    if context.related_memories:
        lines = [f"- {u.content}" for u in context.related_memories if u.content]
        if lines:
            parts.append(
                "## Existing related memories "
                "(already-known facts — do NOT duplicate information these cover):\n"
                + "\n".join(lines)
            )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# ExtractorImpl
# ---------------------------------------------------------------------------


class ExtractorImpl(Extractor):
    """M1 Extractor：3 Phase 流水线——Phase 1 预处理 → Phase 2 LLM 提取 → Phase 3 构建 MemoryUnit。

    tier/tags 由 LLM 在 Phase 2 一并产出，不经 FeatureExtractor 富化。
    L0/L1 分层标注不由本算子负责——由 Evolver 抽取后委托 LayerAnnotator 生成。
    """

    def __init__(
        self,
        llm: LLM,
        min_confidence: float = 0.5,
        retry_max_retries: int = 3,
        retry_backoff_ms: int = 1000,
        extract_batch_size: int = _DEFAULT_EXTRACT_BATCH_SIZE,
    ) -> None:
        self._llm = llm
        self._min_confidence = min_confidence
        self._retry_max_retries = retry_max_retries
        self._retry_backoff_ms = retry_backoff_ms
        self._extract_batch_size = max(1, int(extract_batch_size))

    # ------------------------------------------------------------------
    # 过程记忆抽取（procedural=true：1 条结构化执行历史汇总）
    # ------------------------------------------------------------------

    @staticmethod
    def _is_procedural(units: list[MemoryUnit]) -> bool:
        """本轮是否走过程记忆抽取（metadata["procedural"]=="true"）。"""
        return any(
            str(u.system_metadata.get("procedural", "")).strip().lower() == "true"
            for u in units
        )

    @staticmethod
    def _log_trailing_json_text(text: str, end: int) -> None:
        trailing = text[end:].strip()
        if trailing:
            logger.warning(
                "Extractor: ignored trailing LLM text after JSON root: %s",
                redact_for_log(trailing),
            )

    @staticmethod
    def _strip_non_json(text: str) -> str:
        """去除 markdown fences 等噪声，提取 JSON 核心。"""
        # 剔除 ```json ... ``` 包裹
        s = text.strip()
        if s.startswith("```"):
            lines = s.split("\n")
            # 去首行 ```json 和末行 ```
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            s = "\n".join(lines)
        return s.strip()

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        # 探测 LLM 可用性——若不可用则抛异常
        try:
            self._llm.health()
        except Exception as exc:
            from jiuwen_memory.common.errors import HealthCheckError

            raise HealthCheckError(str(exc)) from exc

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def extract(
        self,
        units: list[MemoryUnit],
        *,
        context: ExtractContext | None = None,
    ) -> list[MemoryUnit]:
        """从一批原始记忆单元中提取零或多条低抽象粒度的派生单元。

        ``context`` 只作 prompt 参考（recent_originals 做指代消解/语境、related_memories
        提示已有事实），不进提取来源。本轮 ``units`` 是唯一提取来源。

        若 ``units`` 标记了 ``metadata["procedural"]=="true"``，走过程记忆抽取：LLM 把
        本轮汇总成 **1 条** 结构化执行历史（PROCEDURAL tier），不走走重、不收 context。
        """
        # 过程记忆模式：产 1 条 PROCEDURAL 执行历史汇总，直接返回（不经 _dedup_batch）。
        if self._is_procedural(units):
            return self._extract_procedural(units)

        # Phase 1: 预处理
        accepted = self._preprocess(units)
        logger.info(
            "Extractor: received %d units, %d accepted after preprocessing",
            len(units),
            len(accepted),
        )
        if not accepted:
            return []

        # Phase 2: LLM 提取（按子批拼 prompt 逐批调用）。
        # 子批之间隔离失败；若仍有合法候选则保留部分成功，否则显式抛出首个错误，
        # 不能把完全失败伪装成模型明确返回 []。
        # tier 与 tags 由 LLM 在此一并产出（同一 prompt），不再走 FeatureExtractor 富化。
        all_candidates: list[ExtractionCandidate] = []
        successful_batches = 0
        failed_batches: list[tuple[int, int, Exception]] = []
        for start in range(0, len(accepted), self._extract_batch_size):
            sub_batch = accepted[start:start + self._extract_batch_size]
            try:
                batch_candidates = self._llm_extract_batch(sub_batch, context=context)
            except Exception as exc:
                failed_batches.append((start, len(sub_batch), exc))
                logger.warning(
                    "Extractor: sub-batch failed and was skipped "
                    "(offset=%d, units=%d, error=%s: %s)",
                    start,
                    len(sub_batch),
                    type(exc).__name__,
                    exc,
                )
                continue
            successful_batches += 1
            all_candidates.extend(batch_candidates)

        if failed_batches and not all_candidates:
            first_error = failed_batches[0][2]
            logger.error(
                "Extractor: no usable candidates after %d successful and %d failed "
                "sub-batches; re-raising first error %s: %s",
                successful_batches,
                len(failed_batches),
                type(first_error).__name__,
                first_error,
            )
            raise first_error
        if failed_batches:
            logger.warning(
                "Extractor: completed with partial success: candidates=%d, "
                "successful_sub_batches=%d, failed_sub_batches=%d",
                len(all_candidates),
                successful_batches,
                len(failed_batches),
            )

        logger.info(
            "Extractor: extracted %d candidates from %d units", len(all_candidates), len(accepted)
        )
        if not all_candidates:
            return []

        # Phase 3: 构建 MemoryUnit（tier/tags 取自 candidate，由 LLM 在 Phase 2 产出）
        # L0/L1 分层标注不由本算子负责——由 Evolver 抽取后委托 LayerAnnotator 生成。
        return self._build_units(all_candidates, accepted)

    # ------------------------------------------------------------------
    # Phase 1: 预处理
    # ------------------------------------------------------------------

    def preprocess(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        return self._preprocess(units)

    def build_candidates(
        self,
        items: list[dict],
        units: list[MemoryUnit],
    ) -> list[ExtractionCandidate]:
        """校验已解析的候选，并绑定到本批 source units。"""
        # 建立 id → unit 索引，用于校验 + 绑定 source
        unit_map = {u.id: u for u in units}

        # 过滤 confidence < min_confidence，按 source_id 绑定到对应 unit
        candidates: list[ExtractionCandidate] = []
        unmatched = 0
        invalid_reasons: list[str] = []
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                invalid_reasons.append(f"item {item_index} is not an object")
                continue
            if "confidence" not in item:
                invalid_reasons.append(f"item {item_index} is missing confidence")
                continue
            try:
                confidence = float(item["confidence"])
            except (TypeError, ValueError):
                invalid_reasons.append(f"item {item_index} has invalid confidence")
                continue
            if not 0.0 <= confidence <= 1.0:
                invalid_reasons.append(f"item {item_index} confidence is outside [0, 1]")
                continue

            source_id = _strip_source_id_shell(str(item.get("source_id", "")))
            if source_id not in unit_map:
                unmatched += 1
                invalid_reasons.append(
                    f"item {item_index} source_id {source_id!r} is not in the batch"
                )
                continue

            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                invalid_reasons.append(f"item {item_index} has empty content")
                continue

            target_str = item.get("target", "fact")
            try:
                target = ExtractionTarget(target_str)
            except ValueError:
                target = ExtractionTarget.FACT

            # tier 由 LLM 判定，限定 episodic/semantic/procedural；非法值兜底 SEMANTIC
            tier = _parse_tier(item.get("tier"))

            # tags 由 LLM 抽，清洗 + 去重 + 截断到 ≤3
            tags = parse_tags(item.get("tags"))

            evidence = item.get("evidence", "")
            if not isinstance(evidence, str):
                invalid_reasons.append(f"item {item_index} has non-string evidence")
                continue
            if confidence < self._min_confidence:
                continue

            event_date = str(item.get("event_date", "") or "").strip()
            candidates.append(ExtractionCandidate(
                target=target,
                content=content.strip(),
                source_unit_id=source_id,
                confidence=confidence,
                evidence=evidence,
                event_date=event_date,
                tier=tier,
                tags=tags,
            ))
            logger.info(
                "Extractor: candidate — source_unit_id=%s tier=%s metadata=%s",
                source_id[:8],
                tier.value,
                metadata_for_log(
                    {
                        "target": target.value,
                        "confidence": confidence,
                        "extracted_statement": content.strip(),
                        "evidence": evidence,
                        "content": content.strip(),
                        "event_date": event_date,
                        "tags": tags,
                    }
                ),
            )

        if invalid_reasons:
            details = "; ".join(invalid_reasons[:8])
            if len(invalid_reasons) > 8:
                details += f"; +{len(invalid_reasons) - 8} more"
            if not candidates:
                raise InvalidExtractionCandidateError(details)
            logger.warning(
                "Extractor: skipped %d invalid candidates while preserving %d valid "
                "candidates: %s",
                len(invalid_reasons),
                len(candidates),
                details,
            )

        logger.info(
            "Extractor: from batch of %d units extracted %d candidates "
            "(raw LLM items=%d, unmatched=%d)",
            len(units),
            len(candidates),
            len(items),
            unmatched,
        )
        return candidates

    def call_llm_with_retry(self, messages: list, max_tokens: int = 8192) -> str:
        return self._call_llm_with_retry(messages, max_tokens=max_tokens)

    def parse_llm_response(self, response: str) -> list[dict]:
        """从响应中恢复最靠左的非空 JSON 对象或数组。"""
        cleaned = self._strip_non_json(response)
        decoder = json.JSONDecoder()
        empty_json_seen = False
        start = 0

        while True:
            array_start = cleaned.find("[", start)
            object_start = cleaned.find("{", start)
            starts = [position for position in (array_start, object_start) if position >= 0]
            if not starts:
                break

            start = min(starts)
            try:
                parsed, end = decoder.raw_decode(cleaned, start)
            except json.JSONDecodeError:
                start += 1
                continue

            if isinstance(parsed, list):
                if parsed:
                    self._log_trailing_json_text(cleaned, end)
                    return parsed
                empty_json_seen = True
            elif isinstance(parsed, dict):
                if parsed:
                    self._log_trailing_json_text(cleaned, end)
                    return [parsed]
                empty_json_seen = True
            start += 1

        if empty_json_seen:
            return []

        try:
            parsed, _ = decoder.raw_decode(cleaned)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Extractor: LLM response is not valid JSON; raw_response=%s",
                redact_for_log(cleaned),
            )
            raise InvalidExtractionJSONError("extractor response is not valid JSON") from exc
        raise InvalidExtractionJSONError(
            f"extractor response must be a JSON array or object, got {type(parsed).__name__}"
        )

    # ------------------------------------------------------------------
    # Phase 3: 构建 MemoryUnit
    # ------------------------------------------------------------------

    def build_units(
        self,
        candidates: list[ExtractionCandidate],
        source_units: list[MemoryUnit],
    ) -> list[MemoryUnit]:
        return self._build_units(candidates, source_units)

    def _extract_procedural(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """把本轮汇总成 1 条 PROCEDURAL 执行历史 MemoryUnit。

        不走走重、不收 context。LLM 产 1 条结构化「目标/步骤/结果」汇总，
        provenance 回指本轮 unit（多源合并到一条，provenance 列全部本轮 unit id）。
        """
        from jiuwen_memory.common.type_def import ChatMessage

        # 拼本轮原文（多 unit 合并成一段，作为单个执行历史源）
        source_text = "\n".join(u.content for u in units if u.content).strip()
        if not source_text:
            logger.info("Extractor._extract_procedural: empty source, return []")
            return []

        messages = [
            ChatMessage(role="system", content=_PROCEDURAL_SYSTEM_PROMPT),
            ChatMessage(role="user", content=source_text),
        ]
        try:
            response = self._call_llm_with_retry(messages)
        except Exception as exc:
            logger.warning("Extractor._extract_procedural: LLM call failed, return []: %s", exc)
            return []

        content = self._parse_procedural_response(response)
        if not content:
            logger.info("Extractor._extract_procedural: empty content parsed, return []")
            return []

        # procedural 路径固定产 PROCEDURAL tier，不走 LLM tier/tags 判定、不走富化。
        candidate = ExtractionCandidate(
            target=ExtractionTarget.CONTEXT,
            content=content,
            source_unit_id=units[0].id,
            confidence=1.0,
            tier=MemoryTier.PROCEDURAL,
        )

        # 构建 1 条 PROCEDURAL MemoryUnit，provenance 回指全部本轮 unit
        now = datetime.now(timezone.utc)
        source = units[0]
        # 合并 write tags（engine 已写到源 unit）+ 系统标记 procedural
        tags = merge_unit_tags(source.tags, ["procedural"])
        unit = MemoryUnit(
            id=str(uuid.uuid4()),
            scope=source.scope,
            tier=MemoryTier.PROCEDURAL,
            segments=[Segment(content=candidate.content, source=source.source)],
            # 不设 source_ref：procedural 原文不落 KV，source.id 指向不存在的记录，
            # 设了反而误导溯源。provenance 仍记本轮 unit id（血缘列表，可指向未落盘源）。
            temporal=Temporal(
                t_event=None,
                t_ingest=now,
                t_valid=now,
                t_message=source.temporal.t_message,
            ),
            provenance=[u.id for u in units],
            tags=tags,
            system_metadata=inherited_system_metadata(units)
            | {
                "confidence": str(candidate.confidence),
                "target": candidate.target.value,
            }
            | candidate.metadata,
            user_metadata=inherited_user_metadata(units),
            lifecycle=LifecycleState.ACTIVE,
        )
        logger.info(
            "Extractor._extract_procedural: built 1 PROCEDURAL unit id=%s provenance=%s content=%s",
            unit.id[:8],
            unit.provenance,
            redact_for_log(unit.content),
        )
        return [unit]

    def _parse_procedural_response(self, response: str) -> str:
        """解析 LLM 返回的 {"content": "..."} JSON，取 content 字段。

        多级兜底：直接解析 → 剥 markdown fence 再解析 → 整个响应当 content。
        任一路径解析出 dict 即取 content 返回；都不成则返回原文（保证产 1 条）。
        """
        for candidate_text in (response.strip(), self._strip_non_json(response)):
            try:
                parsed = json.loads(candidate_text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return str(parsed.get("content", "")).strip()
        logger.warning(
            "Extractor._parse_procedural_response: cannot parse as JSON, use raw response: %s",
            redact_for_log(response),
        )
        return response.strip()

    def _preprocess(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """过滤 lifecycle≠ACTIVE / 空 content / 派生单元（provenance 非空）。"""
        accepted = []
        skipped_reasons: dict[str, int] = {}
        for u in units:
            if u.lifecycle != LifecycleState.ACTIVE:
                skipped_reasons["lifecycle"] = skipped_reasons.get("lifecycle", 0) + 1
                continue
            if not u.content.strip():
                skipped_reasons["empty_content"] = skipped_reasons.get("empty_content", 0) + 1
                continue
            if u.provenance:
                skipped_reasons["provenance"] = skipped_reasons.get("provenance", 0) + 1
                continue  # 跳过派生单元，避免反复提取
            logger.info(
                "Extractor: accepting unit id=%s tier=%s provenance=%s content=%s",
                u.id[:8],
                u.tier.value,
                u.provenance,
                redact_for_log(u.content),
            )
            accepted.append(u)
        logger.info("Extractor: preprocess accepted=%d, skipped=%s", len(accepted), skipped_reasons)
        return accepted

    # ------------------------------------------------------------------
    # Phase 2: LLM 提取（批量）
    # ------------------------------------------------------------------

    def _llm_extract_batch(
        self,
        units: list[MemoryUnit],
        *,
        context: ExtractContext | None = None,
    ) -> list[ExtractionCandidate]:
        """对一批 unit 调一次 LLM 提取——每条 unit 带 [ID: ...] 标识，
        LLM 在每个候选里输出 ``source_id`` 回指来源；解析时校验 source_id 存在。

        ``context`` 非空时在 user prompt 末尾追加两段参考项（见 ExtractContext）：
        - Recent conversation context：做指代/代词消解与语境，不从中提取、不作 source_id。
        - Existing related memories：已有事实，本轮不要重复产出。
        """
        from jiuwen_memory.common.type_def import ChatMessage

        # 拼 user prompt：每条 unit 带 [ID: unit.id] 前缀
        parts = [_SOURCE_PREFIX.format(unit_id=u.id, unit_content=u.content) for u in units]
        user_text = "\n".join(parts)
        # 基准时间 observation_date（经 metadata 下推）：LLM 据此把相对时间解析成绝对时间。
        # 取首个含 observation_date 的 unit（同批通常一致）；未传则以当前时间为基准。
        observation_date = next(
            (
                str(u.system_metadata.get("observation_date", "")).strip()
                for u in units
                if u.system_metadata.get("observation_date")
            ),
            "",
        )
        if not observation_date:
            observation_date = datetime.now(timezone.utc).date().isoformat()
        # prompt 声明该行形如 "observation_date: YYYY-MM-DD"，且据此把观测日期按日精度写进
        # content。调用方可能下推完整时间戳，故统一截到日——否则 prompt 的日精度约定不成立，
        # LLM 会把时分秒当作源文本给出的精度照抄进条目。
        elif "T" in observation_date:
            observation_date = observation_date.split("T", 1)[0]
        user_text = (
            f"observation_date: {observation_date}\n"
            f"(Use this as the reference point to resolve relative time expressions "
            f"like \"yesterday\"/\"last week\"/\"tomorrow\" into absolute dates in content "
            f"and event_date.)\n\n"
            + user_text
        )
        # 追加 context 参考段（仅本轮 units 作提取来源，context 只供 LLM 参考）
        context_block = _format_context_block(context)
        if context_block:
            user_text = user_text + "\n" + context_block
        fidelity_mode = any(
            str(unit.system_metadata.get("extraction_fidelity_mode", "false")).strip().lower()
            in {"true", "1", "yes", "on"}
            for unit in units
        )
        system_prompt = _EXTRACT_SYSTEM_PROMPT
        if fidelity_mode:
            system_prompt = system_prompt + "\n\n" + _EXTRACTION_FIDELITY_SUFFIX
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_text),
        ]

        # 调用 LLM（含重试）
        response = self._call_llm_with_retry(messages)

        # 解析 JSON。
        items = self.parse_llm_response(response)
        return self.build_candidates(items, units)

    def _call_llm_with_retry(self, messages: list, max_tokens: int = 8192) -> str:
        """调用 LLM.chat()，含重试逻辑。"""
        import time

        last_exc = None
        for attempt in range(self._retry_max_retries):
            try:
                return self._llm.chat(messages, temperature=0, max_tokens=max_tokens)
            except Exception as exc:
                last_exc = exc
                if attempt < self._retry_max_retries - 1:
                    wait = self._retry_backoff_ms * (2**attempt) / 1000.0
                    logger.warning(
                        "Extractor: LLM call failed (attempt %d), retrying in %.1fs",
                        attempt + 1,
                        wait,
                    )
                    time.sleep(wait)
        # 所有重试都失败（retry_max_retries <= 0 时未进入循环，last_exc 仍为 None）
        if last_exc is None:
            raise RuntimeError("LLM 调用未执行：retry_max_retries 必须 >= 1")
        raise last_exc

    def _build_units(
        self,
        candidates: list[ExtractionCandidate],
        source_units: list[MemoryUnit],
    ) -> list[MemoryUnit]:
        """将 ExtractionCandidate 转换为 MemoryUnit。

        tier 取自 candidate（由 LLM 在 Phase 2 产出）；不再走 FeatureExtractor 富化。
        tags = write tags（source.tags）∪ LLM 主题 tags ∪ ``extracted`` 派生来源标记
        （与 keyword_extractor / 默认路径 classifier 合并策略对齐，保证 write(tags=...)
        在 infer 派生 unit 上可检索过滤）。
        """
        # 建立 source_unit_id → source_unit 的索引
        source_map = {u.id: u for u in source_units}

        result = []
        seen: set[tuple[str, str]] = set()
        for c in candidates:
            source = source_map.get(c.source_unit_id)
            if source is None:
                # source_id 不匹配——逐条提取下不应出现，但防御性跳过
                logger.warning(
                    "Extractor: source_unit_id %s not found, skipping candidate", c.source_unit_id
                )
                continue

            # write tags 优先，再并 LLM 主题 tags，最后补 extracted 标记
            tags = merge_unit_tags(source.tags, c.tags, ["extracted"])

            statement = c.content.strip()
            if not statement:
                continue
            dedup_key = (source.id, " ".join(statement.split()).casefold())
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            now = datetime.now(timezone.utc)
            unit = MemoryUnit(
                id=str(uuid.uuid4()),
                scope=source.scope,
                tier=c.tier,
                segments=[Segment(content=statement, source=source.source)],
                source_ref=source.id,
                temporal=Temporal(
                    t_event=_parse_event_date(c.event_date),
                    t_ingest=now,
                    t_valid=now,
                    t_message=source.temporal.t_message,
                ),
                provenance=[source.id],
                tags=tags,
                system_metadata=inherited_system_metadata([source])
                | {
                    "confidence": str(c.confidence),
                    "target": c.target.value,
                    "evidence": c.evidence,
                    "extracted_statement": statement,
                }
                | c.metadata,
                user_metadata=inherited_user_metadata([source]),
                lifecycle=LifecycleState.ACTIVE,
            )
            result.append(unit)
            logger.info(
                "Extractor: built unit id=%s tier=%s tags=%s provenance=%s content=%s",
                unit.id[:8],
                unit.tier.value,
                metadata_for_log(unit.tags),
                unit.provenance,
                redact_for_log(unit.content),
            )

        logger.info(
            "Extractor: _build_units produced %d MemoryUnits from %d candidates",
            len(result),
            len(candidates),
        )
        return result


# -- 注册到 ExtractorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@ExtractorProducer.register("llm")
def _build(config):
    return ExtractorImpl(
        llm=LlmProducer.dep(config, default="echo"),
        min_confidence=config.get("extractor_min_confidence", 0.5),
        retry_max_retries=config.get("extractor_retry_max", 3),
        retry_backoff_ms=config.get("extractor_retry_backoff", 1000),
        extract_batch_size=config.get("extract_batch_size", _DEFAULT_EXTRACT_BATCH_SIZE),
    )
