"""LLMAbstractor — M5 信息抽象实现（4 Phase 流水线）。

四阶段流水线（接口契约见 docs/specs/S05-construction.md Abstractor 节）：
  Phase 0  窗口筛选与分组 — 按 tier/数量路由 AbstractionTarget（SUMMARY/PATTERN/PORTRAIT）
  Phase 1  预处理 — 过滤 lifecycle≠ACTIVE / 空 content / 高 tier
  Phase 2  LLM 抽象 — 按组构建 target-specific prompt → LLM.chat()
             → 解析 JSON → AbstractionCandidate
  Phase 3  特征富化 — FeatureExtractor.extract_batch → 补充 keywords/entities
  Phase 4  构建 MemoryUnit — candidate → MemoryUnit(provenance=[source_ids])

三种抽象路径：
  SUMMARY  — 情景→语义概括（EPISODIC→SEMANTIC）
  PATTERN  — 经验→技能/模式提炼（多条EPISODIC→PROCEDURAL）
  PORTRAIT — 长期→画像/偏好聚合（多条SEMANTIC→CORE）

纯函数：不落盘、不标记、不检索。幂等性依赖 LLM temperature=0。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from jiuwen_memory.common.feature_extractor.base import FeatureExtractor, FeatureExtractorProducer
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger
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
from jiuwen_memory.construction.abstractor import AbstractorProducer

from ..abstractor import Abstractor
from ..base import OperatorType

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 内部类型（实现层，不暴露到 __init__.py）
# ---------------------------------------------------------------------------


class AbstractionTarget(str, Enum):
    """LLM 产出的抽象目标类型。"""

    SUMMARY = "summary"  # 情景→语义概括
    PATTERN = "pattern"  # 经验→技能/模式提炼
    PORTRAIT = "portrait"  # 长期→画像/偏好聚合


@dataclass
class AbstractionCandidate:
    """LLM 产出的单条抽象候选项。"""

    target: AbstractionTarget = AbstractionTarget.SUMMARY
    content: str = ""
    source_unit_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence: str = ""
    pattern_name: str = ""  # PATTERN 独有
    portrait_category: str = ""  # PORTRAIT 独有
    keywords: list[str] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# tier → AbstractionTarget 路由
# ---------------------------------------------------------------------------

_TIER_TARGET_MAP: dict[AbstractionTarget, MemoryTier] = {
    AbstractionTarget.SUMMARY: MemoryTier.SEMANTIC,
    AbstractionTarget.PATTERN: MemoryTier.PROCEDURAL,
    AbstractionTarget.PORTRAIT: MemoryTier.CORE,
}


# ---------------------------------------------------------------------------
# LLM prompt — 三种抽象路径的 system prompt
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM_PROMPT = """\
Synthesize a concise semantic statement from the episodic/semantic memories below.
Output ONLY a JSON array. No explanation, no markdown fences.

Rules:
- Combine overlapping facts; eliminate redundancy.
- Produce a self-contained, general statement that captures the essence.
- Language: write the synthesized statement in the SAME language as the input memories
  (Chinese input → Chinese output; English input → English output; mixed → follow the
  dominant language). Never translate to another language.
- "evidence": key phrases from source memories that support the synthesis.
- "confidence": 1.0 = directly supported by all sources, 0.7 = implied by most,
  0.5 = weakly implied. Do not output below 0.5.
- If the memories are too disparate to synthesize meaningfully, return [].

Output schema:
[{
  "target": "summary",
  "content": "self-contained synthesized statement",
  "evidence": "key source phrases",
  "confidence": 0.5~1.0
}]
"""

_PATTERN_SYSTEM_PROMPT = """\
Analyze the repeated experiences below and extract reusable patterns, skills, or strategies.
Output ONLY a JSON array. No explanation, no markdown fences.

Rules:
- Identify what action/approach consistently works or fails across these experiences.
- Output a "pattern" as a concise procedural description.
- Language: write the pattern in the SAME language as the input memories
  (Chinese input → Chinese output; English input → English output; mixed → follow the
  dominant language). Never translate to another language.
- "pattern_name": short descriptive name.
- "evidence": specific moments that demonstrate this pattern.
- "confidence": 1.0 = consistently demonstrated, 0.7 = frequently, 0.5 = occasionally.
  Do not output below 0.5.
- If no clear pattern emerges, return [].

Output schema:
[{
  "target": "pattern",
  "content": "concise procedural description",
  "pattern_name": "short descriptive name",
  "evidence": "specific supporting moments",
  "confidence": 0.5~1.0
}]
"""

_PORTRAIT_SYSTEM_PROMPT = """\
Aggregate the user's long-term preferences, traits, and tendencies from the memories below.
Output ONLY a JSON array. No explanation, no markdown fences.

Rules:
- Identify stable preferences, behavioral tendencies, or personality traits.
- Language: write the portrait in the SAME language as the input memories
  (Chinese input → Chinese output; English input → English output; mixed → follow the
  dominant language). Never translate to another language.
- "category": domain of the trait (communication / work_style / technical / lifestyle / ...).
- "confidence": 1.0 = consistently shown, 0.7 = frequently shown, 0.5 = occasionally.
  Do not output below 0.5.
- If no stable portrait emerges, return [].

Output schema:
[{
  "target": "portrait",
  "content": "stable preference/trait description",
  "category": "domain classification",
  "evidence": "supporting memory references",
  "confidence": 0.5~1.0
}]
"""

_SOURCE_PREFIX = """\
---
[ID: {unit_id}]
{unit_content}
---
"""


# ---------------------------------------------------------------------------
# LLMAbstractor
# ---------------------------------------------------------------------------


class LLMAbstractor(Abstractor):
    """M5 Abstractor：4 Phase 流水线，Phase 2 调用注入的 LLM 插件做语义抽象。"""

    def __init__(
        self,
        llm: LLM,
        feature_extractor: FeatureExtractor,
        min_confidence: float = 0.5,
        min_group_size_summary: int = 1,
        min_group_size_pattern: int = 3,
        min_group_size_portrait: int = 5,
        max_groups_per_batch: int = 4,
        max_context_tokens: int = 180000,
        retry_max_retries: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        """初始化 LLMAbstractor。

        Args:
            llm: 参数 llm（LLM）。
            feature_extractor: 参数 feature_extractor（FeatureExtractor）。
            min_confidence: 参数 min_confidence（float）。
            min_group_size_summary: 参数 min_group_size_summary（int）。
            min_group_size_pattern: 参数 min_group_size_pattern（int）。
            min_group_size_portrait: 参数 min_group_size_portrait（int）。
            max_groups_per_batch: 参数 max_groups_per_batch（int）。
            max_context_tokens: 参数 max_context_tokens（int）。
            retry_max_retries: 参数 retry_max_retries（int）。
            retry_backoff_ms: 参数 retry_backoff_ms（int）。
        """
        self._llm = llm
        self._feature_extractor = feature_extractor
        self._min_confidence = min_confidence
        self._min_group_size_summary = min_group_size_summary
        self._min_group_size_pattern = min_group_size_pattern
        self._min_group_size_portrait = min_group_size_portrait
        self._max_groups_per_batch = max_groups_per_batch
        self._max_context_tokens = max_context_tokens
        self._retry_max_retries = retry_max_retries
        self._retry_backoff_ms = retry_backoff_ms
        # 估算 token 系数：4 chars ≈ 1 token（中英文混合场景）
        self._chars_per_token = 4

    @staticmethod
    def _strip_non_json(text: str) -> str:
        """去除 markdown fences 等噪声，提取 JSON 核心。"""
        s = text.strip()
        if s.startswith("```"):
            lines = s.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            s = "\n".join(lines)
        return s.strip()

    def operator_type(self) -> OperatorType:
        """返回当前算子类型。

        Returns:
            返回 OperatorType。
        """
        return OperatorType.ABSTRACTOR

    def health(self) -> None:
        # 探测 LLM 可用性——若不可用则抛异常
        """执行健康检查。

        Raises:
            HealthCheckError: 执行失败时抛出。
        """
        try:
            self._llm.health()
        except Exception as exc:
            from jiuwen_memory.common.errors import HealthCheckError

            raise HealthCheckError(str(exc)) from exc

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def abstract(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """对一批记忆单元做抽象与精炼，产出高抽象粒度的新记忆单元。"""
        logger.info("Abstractor: received %d units", len(units))
        for u in units:
            logger.info(
                "Abstractor: input unit id=%s lifecycle=%s tier=%s provenance=%s content=%s",
                u.id[:8],
                u.lifecycle.value,
                u.tier.value,
                u.provenance,
                u.content[:200],
            )

        # Phase 1: 预处理
        accepted = self._preprocess(units)
        if not accepted:
            return []

        # Phase 0: 窗口筛选与分组
        groups = self._group_by_target(accepted)
        if not groups:
            logger.info("Abstractor: no groups formed, returning empty")
            return []
        logger.info(
            "Abstractor: formed %d groups — %s",
            len(groups),
            [(g[0].value, len(g[1])) for g in groups],
        )

        # Phase 2: LLM 抽象（按分组）
        all_candidates: list[AbstractionCandidate] = []
        for group_target, group_units in groups:
            logger.info(
                "Abstractor: LLM abstracting group target=%s, %d units",
                group_target.value,
                len(group_units),
            )
            try:
                candidates = self._llm_abstract(group_target, group_units)
                all_candidates.extend(candidates)
                logger.info(
                    "Abstractor: group target=%s produced %d candidates",
                    group_target.value,
                    len(candidates),
                )
            except Exception:
                logger.warning(
                    "Abstractor: LLM group failed (target=%s, %d units), skipping",
                    group_target.value,
                    len(group_units),
                )
                # 失败隔离：单个 group 失败不影响其余
                continue

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
        """过滤 lifecycle≠ACTIVE / 空 content / 高 tier（CORE/ARCHIVAL 不需再抽象）。"""
        accepted = []
        skipped_reasons: dict[str, int] = {}
        for u in units:
            if u.lifecycle != LifecycleState.ACTIVE:
                skipped_reasons["lifecycle"] = skipped_reasons.get("lifecycle", 0) + 1
                continue
            if not u.content.strip():
                skipped_reasons["empty_content"] = skipped_reasons.get("empty_content", 0) + 1
                continue
            logger.info(
                "Abstractor: accepting unit id=%s tier=%s content=%s",
                u.id[:8],
                u.tier.value,
                u.content[:200],
            )
            accepted.append(u)
        logger.info(
            "Abstractor: preprocess accepted=%d, skipped=%s", len(accepted), skipped_reasons
        )
        return accepted

    # ------------------------------------------------------------------
    # Phase 0: 窗口筛选与分组
    # ------------------------------------------------------------------

    def _group_by_target(
        self, units: list[MemoryUnit]
    ) -> list[tuple[AbstractionTarget, list[MemoryUnit]]]:
        """按 tier/数量路由 AbstractionTarget，超出 token 限制时做代表性选择。

        分组策略（M5 阶段，基于 tier 的简单规则，不依赖 Embedder 聚类）：
          - 多条 SEMANTIC 且 ≥ min_group_size_portrait → PORTRAIT
          - 多条 EPISODIC 且 ≥ min_group_size_pattern  → PATTERN
          - 其余 → SUMMARY
        """
        if not units:
            return []

        # 按 tier 分桶
        episodic_units = [u for u in units if u.tier == MemoryTier.EPISODIC]
        semantic_units = [u for u in units if u.tier == MemoryTier.SEMANTIC]
        other_units = [u for u in units if u.tier not in (MemoryTier.EPISODIC, MemoryTier.SEMANTIC)]

        groups: list[tuple[AbstractionTarget, list[MemoryUnit]]] = []

        # PORTRAIT：多条 SEMANTIC（长期偏好聚合）
        if len(semantic_units) >= self._min_group_size_portrait:
            portrait_group = self._representative_select(semantic_units, AbstractionTarget.PORTRAIT)
            groups.append((AbstractionTarget.PORTRAIT, portrait_group))
            # 消费已分组的 semantic_units，剩余走 SUMMARY
            remaining_semantic = [u for u in semantic_units if u not in portrait_group]
            if remaining_semantic:
                groups.append((AbstractionTarget.SUMMARY, remaining_semantic))
        elif semantic_units:
            # SEMANTIC 数量不足 PORTRAIT，走 SUMMARY
            groups.append((AbstractionTarget.SUMMARY, semantic_units))

        # PATTERN：多条 EPISODIC（经验→技能/模式）
        if len(episodic_units) >= self._min_group_size_pattern:
            pattern_group = self._representative_select(episodic_units, AbstractionTarget.PATTERN)
            groups.append((AbstractionTarget.PATTERN, pattern_group))
            # 消费已分组的 episodic_units，剩余走 SUMMARY
            remaining_episodic = [u for u in episodic_units if u not in pattern_group]
            if remaining_episodic:
                groups.append((AbstractionTarget.SUMMARY, remaining_episodic))
        elif episodic_units:
            # EPISODIC 数量不足 PATTERN，走 SUMMARY
            groups.append((AbstractionTarget.SUMMARY, episodic_units))

        # 其它 tier 一律走 SUMMARY
        if other_units:
            groups.append((AbstractionTarget.SUMMARY, other_units))

        # 限制并发分组数量
        if len(groups) > self._max_groups_per_batch:
            groups = groups[: self._max_groups_per_batch]

        return groups

    def _representative_select(
        self, units: list[MemoryUnit], target: AbstractionTarget
    ) -> list[MemoryUnit]:
        """超出 LLM 上下文窗时做代表性选择：均匀取样确保时间覆盖面。"""
        total_tokens = sum(len(u.content) // self._chars_per_token for u in units)
        # 加上 prompt overhead 估算（system prompt ≈ 500 tokens + format overhead）
        prompt_overhead = 500
        available_tokens = self._max_context_tokens - prompt_overhead

        if total_tokens <= available_tokens:
            return units

        # 超出限制：均匀取样
        # 估算可容纳的 unit 数量
        avg_tokens = total_tokens / len(units) if units else 1
        max_units = max(1, int(available_tokens / avg_tokens))

        # 均匀取样：每隔 N 条取 1 条，确保时间分布覆盖
        step = len(units) / max_units
        selected = []
        for i in range(max_units):
            idx = int(i * step)
            if idx < len(units):
                selected.append(units[idx])

        logger.info(
            "Abstractor: representative select for %s — %d/%d units "
            "(estimated %d tokens > %d limit)",
            target.value,
            len(selected),
            len(units),
            total_tokens,
            self._max_context_tokens,
        )
        return selected

    # ------------------------------------------------------------------
    # Phase 2: LLM 抽象
    # ------------------------------------------------------------------

    def _llm_abstract(
        self, target: AbstractionTarget, units: list[MemoryUnit]
    ) -> list[AbstractionCandidate]:
        """构建 target-specific prompt → 调 LLM → 解析 JSON → 过滤 confidence。"""

        # 选择 system prompt
        system_prompt = self._get_system_prompt(target)

        # 构建 user prompt
        parts = []
        for u in units:
            parts.append(_SOURCE_PREFIX.format(unit_id=u.id, unit_content=u.content))
        user_text = "\n".join(parts)

        from jiuwen_memory.common.type_def import ChatMessage

        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_text),
        ]

        # 调用 LLM（含重试）
        response = self._call_llm_with_retry(messages)

        # 解析 JSON
        items = self._parse_llm_response(response)

        # 过滤 confidence < min_confidence，构建 AbstractionCandidate
        candidates = []
        for item in items:
            confidence = float(item.get("confidence", 0.0))
            if confidence < self._min_confidence:
                continue

            target_str = item.get("target", target.value)
            try:
                parsed_target = AbstractionTarget(target_str)
            except ValueError:
                parsed_target = target  # 回退到本组的默认 target

            candidates.append(
                AbstractionCandidate(
                    target=parsed_target,
                    content=item.get("content", ""),
                    source_unit_ids=[u.id for u in units],
                    confidence=confidence,
                    evidence=item.get("evidence", ""),
                    pattern_name=item.get("pattern_name", ""),
                    portrait_category=item.get("category", ""),
                )
            )
            logger.info(
                "Abstractor: candidate — target=%s, confidence=%.2f, content=%s, evidence=%s",
                parsed_target.value,
                confidence,
                item.get("content", "")[:200],
                item.get("evidence", "")[:100],
            )

        return candidates

    def _get_system_prompt(self, target: AbstractionTarget) -> str:
        """按 AbstractionTarget 获取对应的 LLM system prompt。"""
        if target == AbstractionTarget.SUMMARY:
            return _SUMMARY_SYSTEM_PROMPT
        elif target == AbstractionTarget.PATTERN:
            return _PATTERN_SYSTEM_PROMPT
        elif target == AbstractionTarget.PORTRAIT:
            return _PORTRAIT_SYSTEM_PROMPT
        else:
            return _SUMMARY_SYSTEM_PROMPT  # 降级为 SUMMARY

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
                        "Abstractor: LLM call failed (attempt %d), retrying in %.1fs",
                        attempt + 1,
                        wait,
                    )
                    time.sleep(wait)
        # 所有重试都失败（retry_max_retries <= 0 时未进入循环，last_exc 仍为 None）
        if last_exc is None:
            raise RuntimeError("LLM 调用未执行：retry_max_retries 必须 >= 1")
        raise last_exc

    def _parse_llm_response(self, response: str) -> list[dict]:
        """解析 LLM 返回的 JSON，失败时尝试提取 JSON 核心部分。"""
        # 尝试直接解析
        try:
            parsed = json.loads(response)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            logger.debug("Abstractor: direct JSON parse failed, trying stripped JSON")

        # 解析失败：尝试提取 JSON 部分（去除 markdown fences 等噪声）
        cleaned = self._strip_non_json(response)
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            logger.warning("Abstractor: LLM response not valid JSON, returning empty")
            return []
        return []

    # ------------------------------------------------------------------
    # Phase 3: 特征富化
    # ------------------------------------------------------------------

    def _enrich_features(self, candidates: list[AbstractionCandidate]) -> None:
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
            logger.warning("Abstractor: FeatureExtractor unavailable, skipping enrichment")

    # ------------------------------------------------------------------
    # Phase 4: 构建 MemoryUnit
    # ------------------------------------------------------------------

    def _build_units(
        self,
        candidates: list[AbstractionCandidate],
        source_units: list[MemoryUnit],
    ) -> list[MemoryUnit]:
        """将 AbstractionCandidate 转换为 MemoryUnit。"""
        # 建立 source_unit_id → source_unit 的索引
        source_map = {u.id: u for u in source_units}

        result = []
        for c in candidates:
            # 验证所有 source_unit_ids 存在于预处理后的 accepted 集中
            valid_source_ids = [sid for sid in c.source_unit_ids if sid in source_map]
            if not valid_source_ids:
                logger.warning("Abstractor: no valid source_unit_ids for candidate, skipping")
                continue

            # 取第一个有效 source 作为 scope/temporal 的基准
            primary_source = source_map[valid_source_ids[0]]

            # tier 由 AbstractionTarget 决定
            tier = _TIER_TARGET_MAP.get(c.target, MemoryTier.SEMANTIC)

            # 构建 metadata（含 target-specific 额外字段）
            metadata: dict[str, str] = {
                "confidence": str(c.confidence),
                "target": c.target.value,
                "evidence": c.evidence,
            }
            if c.pattern_name:
                metadata["pattern_name"] = c.pattern_name
            if c.portrait_category:
                metadata["portrait_category"] = c.portrait_category
            metadata.update(c.metadata)

            unit = MemoryUnit(
                id=str(uuid.uuid4()),
                scope=primary_source.scope,
                tier=tier,
                segments=[Segment(content=c.content, source=primary_source.source)],
                source_ref=primary_source.id,
                temporal=Temporal(
                    t_event=primary_source.temporal.t_event,
                    t_ingest=datetime.now(timezone.utc),
                    t_message=primary_source.temporal.t_message,
                ),
                provenance=valid_source_ids,
                tags=c.keywords,
                system_metadata=inherited_system_metadata(
                    [source_map[source_id] for source_id in valid_source_ids]
                )
                | metadata,
                user_metadata=inherited_user_metadata(
                    [source_map[source_id] for source_id in valid_source_ids]
                ),
                lifecycle=LifecycleState.ACTIVE,
            )
            result.append(unit)
            logger.info(
                "Abstractor: built unit id=%s tier=%s provenance=%s content=%s",
                unit.id[:8],
                unit.tier.value,
                unit.provenance,
                unit.content[:200],
            )

        logger.info(
            "Abstractor: _build_units produced %d MemoryUnits from %d candidates",
            len(result),
            len(candidates),
        )
        return result


# -- 注册到 AbstractorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@AbstractorProducer.register("llm")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return LLMAbstractor(
        llm=LlmProducer.dep(config, default="echo"),
        feature_extractor=FeatureExtractorProducer.dep(config, default="keyword"),
        min_confidence=config.get("abstractor_min_confidence", 0.5),
        min_group_size_summary=config.get("abstractor_min_group_size_summary", 1),
        min_group_size_pattern=config.get("abstractor_min_group_size_pattern", 3),
        min_group_size_portrait=config.get("abstractor_min_group_size_portrait", 5),
        max_groups_per_batch=config.get("abstractor_max_groups_per_batch", 4),
        max_context_tokens=config.get("abstractor_max_context_tokens", 180000),
        retry_max_retries=config.get("abstractor_retry_max", 3),
        retry_backoff_ms=config.get("abstractor_retry_backoff", 1000),
    )
