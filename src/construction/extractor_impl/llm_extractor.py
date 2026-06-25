"""ExtractorImpl — M1 信息提取实现（4 Phase 流水线）。

四阶段流水线（接口契约见 docs/specs/S05-construction.md Extractor 节）：
  Phase 1  预处理 — 过滤 lifecycle≠ACTIVE / 空 content
  Phase 2  LLM 提取 — 逐条 unit 调 LLM → 解析 JSON → ExtractionCandidate
  Phase 3  特征富化 — FeatureExtractor.extract_batch → 补充 keywords/entities
  Phase 4  构建 MemoryUnit — candidate → MemoryUnit(provenance=[source_id])

Phase 2 采用逐条提取策略：每条 MemoryUnit 单独调一次 LLM，
source_unit_id 直接取当前 unit 的 id，无需 LLM 输出 source_id，
也无需事后匹配路由——彻底消除批量场景下 source_id 丢失/错配问题。

纯函数：不落盘、不标记、不检索。幂等性依赖 LLM temperature=0。
"""

from __future__ import annotations

import json
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
# LLM prompt — 逐条提取，无需 source_id / [ID: xxx] 标记
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM_PROMPT = """\
Extract facts, events, and preferences from the text below.
Output ONLY a JSON array. No explanation, no markdown fences.

Rules:
- Extract only what is explicitly stated. Do not infer or speculate.
- Each item must be self-contained (understandable without source context).
- Language: write each extracted statement in the SAME language as the source text
  (Chinese source → Chinese statement; English source → English statement). Never
  translate the extracted content to another language.
- "evidence": exact quote from the source text.
- "confidence": 1.0 = directly stated, 0.7 = clearly implied,
  0.5 = weakly implied. Do not extract below 0.5.
- If nothing worth extracting, return [].

Target types:
- "fact": a stated truth or piece of information
- "event": something that happened at a point in time
- "preference": the user likes/dislikes/prefers/wants something

Output schema:
[{
  "target": "fact" | "event" | "preference",
  "content": "self-contained statement",
  "evidence": "exact source quote",
  "confidence": 0.5~1.0
}]
"""


# ---------------------------------------------------------------------------
# ExtractorImpl
# ---------------------------------------------------------------------------


class ExtractorImpl(Extractor):
    """M1 Extractor：4 Phase 流水线，Phase 2 逐条 unit 调用注入的 LLM 插件。"""

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

        # Phase 2: LLM 提取（逐条）
        all_candidates: list[ExtractionCandidate] = []
        for u in accepted:
            try:
                candidates = self._llm_extract_single(u)
                all_candidates.extend(candidates)
            except Exception:
                logger.warning("Extractor: LLM extract failed for unit %s, skipping", u.id[:8])
                # 失败隔离：单条 unit 失败不影响其余
                continue

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
    # Phase 2: LLM 提取（逐条）
    # ------------------------------------------------------------------

    def _llm_extract_single(self, unit: MemoryUnit) -> list[ExtractionCandidate]:
        """对单条 unit 调 LLM 提取——source_unit_id 直接取 unit.id。"""

        from common.type_def import ChatMessage

        messages = [
            ChatMessage(role="system", content=_EXTRACT_SYSTEM_PROMPT),
            ChatMessage(role="user", content=unit.content),
        ]

        # 调用 LLM（含重试）
        response = self._call_llm_with_retry(messages)

        # 解析 JSON
        items = self._parse_llm_response(response)

        # 过滤 confidence < min_confidence，并绑定 source_unit_id
        candidates = []
        for item in items:
            confidence = float(item.get("confidence", 0.0))
            if confidence < self._min_confidence:
                continue

            target_str = item.get("target", "fact")
            try:
                target = ExtractionTarget(target_str)
            except ValueError:
                target = ExtractionTarget.FACT

            candidates.append(
                ExtractionCandidate(
                    target=target,
                    content=item.get("content", ""),
                    source_unit_id=unit.id,  # 逐条提取，source 直接确定
                    confidence=confidence,
                    evidence=item.get("evidence", ""),
                )
            )
            logger.info(
                "Extractor: candidate — target=%s, confidence=%.2f, content=%s, evidence=%s",
                target.value,
                confidence,
                item.get("content", "")[:200],
                item.get("evidence", "")[:100],
            )

        logger.info(
            "Extractor: from unit id=%s extracted %d candidates (raw LLM items=%d)",
            unit.id[:8],
            len(candidates),
            len(items),
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
