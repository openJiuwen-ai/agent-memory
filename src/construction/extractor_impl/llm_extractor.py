"""ExtractorImpl — M1 信息提取实现（4 Phase 流水线）。

四阶段流水线（接口契约见 docs/specs/S05-construction.md Extractor 节）：
  Phase 1  预处理 — 过滤 lifecycle≠ACTIVE / 空 content
  Phase 2  LLM 提取 — 批量拼一个 prompt 调 LLM → 解析 JSON → ExtractionCandidate
  Phase 3  特征富化 — FeatureExtractor.extract_batch → 补充 keywords/entities
  Phase 4  构建 MemoryUnit — candidate → MemoryUnit(provenance=[source_id])

Phase 2 采用批量提取策略：全部 unit 拼一个 prompt，每条带 ``[ID: unit_id]`` 标记，
LLM 在每个候选里输出裸 ``source_id`` 回指来源。解析时校验 source_id 存在于本批 unit，
缺失/错配的候选丢弃。``_strip_source_id_shell`` 兜底剥掉 LLM 误带的 ``[ID: ]`` 外壳，
避免 source_id 恒不匹配导致候选全丢。

prompt 要求 LLM 把同一 source 内**同主题**的多个子事实合并成一条自包含陈述（如咖啡
偏好：早上美式/下午拿铁/不加糖 合成 1 条，而非 3 条碎片），减少派生单元数量、提升
每条单元的信息密度。不同主题（咖啡≠编程技能≠会议安排）保持独立。跨次 write 的重复
仍由 Evolver ``_dedup_batch`` 兜底（向量召回判定 NOOP/UPDATE）。

纯函数：不落盘、不标记、不检索。幂等性依赖 LLM temperature=0。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from common.feature_extractor.base import FeatureExtractor, FeatureExtractorProducer
from common.llm.base import LLM, LlmProducer
from common.log import get_logger
from common.type_def import (
    Entity,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Segment,
    Temporal,
)
from construction.extractor import ExtractorProducer

from ..base import OperatorType
from ..extractor import Extractor

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 内部类型（实现层，不暴露到 __init__.py）
# ---------------------------------------------------------------------------


class ExtractionTarget(str, Enum):
    """LLM 产出的提取目标类型。"""

    FACT = "fact"
    EVENT = "event"
    PREFERENCE = "preference"
    CONTEXT = "context"


@dataclass
class ExtractionCandidate:
    """LLM 产出的单条候选项。"""

    target: ExtractionTarget = ExtractionTarget.FACT
    content: str = ""
    source_unit_id: str = ""
    source_span: tuple[int, int] = (0, 0)
    confidence: float = 0.0
    evidence: str = ""
    keywords: list[str] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LLM prompt — 批量提取，每条 unit 带 [ID: uuid] 标记，LLM 输出裸 uuid 作 source_id
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM_PROMPT = """\
Extract facts, events, preferences, and context from the source memories below.
Output ONLY a JSON array. No explanation, no markdown fences.

Rules:
- Extract only what is explicitly stated. Do not infer or speculate.
- Each item must be self-contained (understandable without source context).
- MERGE same-topic facts within one source into a single statement. Facts about the
  same subject/domain (e.g. coffee preferences, a specific skill, a recurring meeting)
  MUST be combined into one item, not split into many. Each item should be a complete,
  self-contained statement covering one topic.
  GOOD: "Alice 的咖啡偏好：早上喝美式、下午喝拿铁，且不加糖" (one item, full topic)
  BAD:  "Alice 早上喝美式咖啡" + "Alice 下午喝拿铁咖啡" + "Alice 喝咖啡不加糖" (three fragmented items)
  But different topics stay separate: coffee preferences ≠ programming skills ≠ meeting schedule.
- "source_id": the bare id of the source memory the item was extracted from.
  Use the UUID value shown inside the [ID: ...] marker — WITHOUT the "[ID: ]" wrapper,
  WITHOUT quotes around the marker, and WITHOUT any leading/trailing whitespace.
  Example: for "--- [ID: 32049cd0-5f7c-419f-928f-503b24318f7c] ---", output
  "source_id": "32049cd0-5f7c-419f-928f-503b24318f7c". Every item MUST include a valid source_id.
  Only merge facts that share the same source_id; never merge across different sources.
- Language: write each extracted statement in the SAME language as its source text
  (Chinese source → Chinese statement; English source → English statement). Never
  translate the extracted content to another language.
- "confidence": 1.0 = directly stated, 0.7 = clearly implied, 0.5 = weakly implied. Do not extract below 0.5.
- If nothing worth extracting, return [].

Target types:
- "fact": a stated truth or piece of information about the user or world
- "event": something that happened at a point in time
- "preference": the user likes/dislikes/prefers/wants something
- "context": any other durable information that may be useful for future conversations
  (e.g., project background, ongoing tasks, skills demonstrated, topics discussed)

Output schema:
[{
  "source_id": "32049cd0-5f7c-419f-928f-503b24318f7c",
  "target": "fact" | "event" | "preference" | "context",
  "content": "self-contained statement (one topic, merged if multiple sub-facts)",
  "confidence": 0.5~1.0
}]
"""

_SOURCE_PREFIX = """\
---
[ID: {unit_id}]
{unit_content}
---
"""


# 匹配 LLM 误带 [ID: ...] 外壳的 source_id（prompt 已要求裸 uuid，此处为防御性兜底）
_SOURCE_ID_SHELL_RE = re.compile(r"^\s*\[ID:\s*(?P<id>[^\]]+)\]\s*$", re.IGNORECASE)

# 批量提取的子批大小：把 accepted 拆成若干子批，每子批独立一次 LLM 调用 + 独立
# try/except——单子批 LLM/解析失败只丢该子批的候选，不波及整批（失败爆炸半径隔离）。
_EXTRACT_BATCH_SIZE = 8


def _strip_source_id_shell(raw: str) -> str:
    """剥掉 source_id 可能带的 ``[ID: ...]`` 外壳，返回裸 id。

    prompt 已要求 LLM 输出裸 uuid，但部分模型仍会原样回带 ``[ID: ...]`` 标记——
    解析端兜底剥壳，避免 source_id 恒不匹配 unit_map 导致候选全被丢弃。
    """
    m = _SOURCE_ID_SHELL_RE.match(raw)
    return m.group("id").strip() if m else raw.strip()


# ---------------------------------------------------------------------------
# ExtractorImpl
# ---------------------------------------------------------------------------


class ExtractorImpl(Extractor):
    """M1 Extractor：4 Phase 流水线，Phase 2 把全部 unit 拼一个 prompt 一次调 LLM。"""

    def __init__(
        self,
        llm: LLM,
        feature_extractor: FeatureExtractor,
        min_confidence: float = 0.5,
        retry_max_retries: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        self._llm = llm
        self._feature_extractor = feature_extractor
        self._min_confidence = min_confidence
        self._retry_max_retries = retry_max_retries
        self._retry_backoff_ms = retry_backoff_ms

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        # 探测 LLM 可用性——若不可用则抛异常
        try:
            self._llm.health()
        except Exception as exc:
            from common.errors import HealthCheckError

            raise HealthCheckError(str(exc)) from exc

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def extract(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """从一批原始记忆单元中提取零或多条低抽象粒度的派生单元。"""

        # Phase 1: 预处理
        accepted = self._preprocess(units)
        logger.info(
            "Extractor: received %d units, %d accepted after preprocessing",
            len(units),
            len(accepted),
        )
        if not accepted:
            return []

        # Phase 2: LLM 提取（按子批拼 prompt 逐批调用，单批失败不波及其余）
        all_candidates: list[ExtractionCandidate] = []
        for start in range(0, len(accepted), _EXTRACT_BATCH_SIZE):
            sub_batch = accepted[start:start + _EXTRACT_BATCH_SIZE]
            try:
                all_candidates.extend(self._llm_extract_batch(sub_batch))
            except Exception:
                logger.warning(
                    "Extractor: LLM extract failed for sub-batch of %d units (offset=%d), skipping",
                    len(sub_batch), start,
                )

        logger.info(
            "Extractor: extracted %d candidates from %d units", len(all_candidates), len(accepted)
        )
        if not all_candidates:
            return []

        # Phase 3: 特征富化
        self._enrich_features(all_candidates)

        # Phase 4: 构建 MemoryUnit
        return self._build_units(all_candidates, accepted)

    # ------------------------------------------------------------------
    # Phase 1: 预处理
    # ------------------------------------------------------------------

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
                u.content[:200],
            )
            accepted.append(u)
        logger.info("Extractor: preprocess accepted=%d, skipped=%s", len(accepted), skipped_reasons)
        return accepted

    # ------------------------------------------------------------------
    # Phase 2: LLM 提取（批量）
    # ------------------------------------------------------------------

    def _llm_extract_batch(self, units: list[MemoryUnit]) -> list[ExtractionCandidate]:
        """对一批 unit 调一次 LLM 提取——每条 unit 带 [ID: ...] 标识，
        LLM 在每个候选里输出 ``source_id`` 回指来源；解析时校验 source_id 存在。
        """
        from common.type_def import ChatMessage

        # 拼 user prompt：每条 unit 带 [ID: unit.id] 前缀
        parts = [_SOURCE_PREFIX.format(unit_id=u.id, unit_content=u.content) for u in units]
        user_text = "\n".join(parts)
        messages = [
            ChatMessage(role="system", content=_EXTRACT_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_text),
        ]

        # 调用 LLM（含重试）
        response = self._call_llm_with_retry(messages)

        # 解析 JSON
        items = self._parse_llm_response(response)

        # 建立 id → unit 索引，用于校验 + 绑定 source
        unit_map = {u.id: u for u in units}

        # 过滤 confidence < min_confidence，按 source_id 绑定到对应 unit
        candidates: list[ExtractionCandidate] = []
        unmatched = 0
        for item in items:
            confidence = float(item.get("confidence", 0.0))
            if confidence < self._min_confidence:
                continue

            source_id = _strip_source_id_shell(str(item.get("source_id", "")))
            if source_id not in unit_map:
                # source_id 不匹配——批量提取下 LLM 可能漏写或写错，跳过该候选
                unmatched += 1
                logger.warning(
                    "Extractor: candidate source_id %r not in batch, skipping (content=%s)",
                    source_id, str(item.get("content", ""))[:80],
                )
                continue

            target_str = item.get("target", "fact")
            try:
                target = ExtractionTarget(target_str)
            except ValueError:
                target = ExtractionTarget.FACT

            evidence = item.get("evidence", "")
            candidates.append(ExtractionCandidate(
                target=target,
                content=item.get("content", ""),
                source_unit_id=source_id,
                confidence=confidence,
                evidence=evidence,
            ))
            ev_str = f", evidence={evidence[:100]}" if evidence else ""
            logger.info(
                "Extractor: candidate — source=%s target=%s, confidence=%.2f, content=%s%s",
                source_id[:8],
                target.value,
                confidence,
                item.get("content", "")[:200],
                ev_str,
            )

        logger.info(
            "Extractor: from batch of %d units extracted %d candidates (raw LLM items=%d, unmatched=%d)",
            len(units),
            len(candidates),
            len(items),
            unmatched,
        )
        return candidates

    def _call_llm_with_retry(self, messages: list) -> str:
        """调用 LLM.chat()，含重试逻辑。"""
        import time

        last_exc = None
        for attempt in range(self._retry_max_retries):
            try:
                return self._llm.chat(messages, temperature=0, max_tokens=4096)
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

    def _parse_llm_response(self, response: str) -> list[dict]:
        """解析 LLM 返回的 JSON。"""
        # 尝试直接解析
        try:
            parsed = json.loads(response)
            if isinstance(parsed, list):
                return parsed
            # 单条包装为 list
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            logger.debug("Extractor: direct JSON parse failed, trying stripped JSON")

        # 解析失败：尝试提取 JSON 部分（去除 markdown fences 等噪声）
        cleaned = self._strip_non_json(response)
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            logger.warning("Extractor: LLM response not valid JSON, returning empty")
            return []
        return []

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

    # ------------------------------------------------------------------
    # Phase 3: 特征富化
    # ------------------------------------------------------------------

    def _enrich_features(self, candidates: list[ExtractionCandidate]) -> None:
        """FeatureExtractor 对 LLM 产出的精炼陈述做关键词/实体抽取。"""
        try:
            texts = [c.content for c in candidates if c.content]
            if not texts:
                return
            features = self._feature_extractor.extract_batch(texts)
            # 对齐：content 为空的 candidate 不参与 extract_batch，需偏移索引
            enrich_idx = 0
            for c in candidates:
                if c.content:
                    if enrich_idx < len(features):
                        f = features[enrich_idx]
                        c.keywords = f.keywords
                        c.entities = f.entities
                        c.metadata.update(f.labels)
                    enrich_idx += 1
        except Exception:
            # FeatureExtractor 不可用时降级：跳过富化
            logger.warning("Extractor: FeatureExtractor unavailable, skipping enrichment")

    # ------------------------------------------------------------------
    # Phase 4: 构建 MemoryUnit
    # ------------------------------------------------------------------

    def _build_units(
        self,
        candidates: list[ExtractionCandidate],
        source_units: list[MemoryUnit],
    ) -> list[MemoryUnit]:
        """将 ExtractionCandidate 转换为 MemoryUnit。"""
        # 建立 source_unit_id → source_unit 的索引
        source_map = {u.id: u for u in source_units}

        result = []
        for c in candidates:
            source = source_map.get(c.source_unit_id)
            if source is None:
                # source_id 不匹配——逐条提取下不应出现，但防御性跳过
                logger.warning(
                    "Extractor: source_unit_id %s not found, skipping candidate", c.source_unit_id
                )
                continue

            # tags = FeatureExtractor 关键词 + extracted 标签（与 keyword_extractor 一致，
            # 标记派生来源，便于后续过滤/避免反复提取）
            tags = list(c.keywords)
            if "extracted" not in tags:
                tags.append("extracted")

            unit = MemoryUnit(
                id=str(uuid.uuid4()),
                scope=source.scope,
                tier=MemoryTier.SEMANTIC,
                segments=[Segment(content=c.content, source=source.source)],
                source_ref=source.id,
                temporal=Temporal(
                    t_event=source.temporal.t_event,
                    t_ingest=datetime.now(timezone.utc),
                ),
                provenance=[source.id],
                tags=tags,
                metadata={
                    "confidence": str(c.confidence),
                    "target": c.target.value,
                    "evidence": c.evidence,
                }
                | c.metadata,
                lifecycle=LifecycleState.ACTIVE,
            )
            result.append(unit)
            logger.info(
                "Extractor: built unit id=%s tier=%s provenance=%s content=%s",
                unit.id[:8],
                unit.tier.value,
                unit.provenance,
                unit.content[:200],
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
        feature_extractor=FeatureExtractorProducer.dep(config, default="keyword"),
        min_confidence=config.get("extractor_min_confidence", 0.5),
        retry_max_retries=config.get("extractor_retry_max", 3),
        retry_backoff_ms=config.get("extractor_retry_backoff", 1000),
    )
