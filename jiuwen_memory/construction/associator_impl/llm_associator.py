"""LLMAssociator — M2 关联分析实现（三层发现流水线）。

三层发现策略（接口契约见 docs/specs/S05-construction.md Associator 节）：
  Phase 1  实体抽取 + 向量化（L1 基础）
           · FeatureExtractor.extract_batch() → entities + keywords
           · Embedder.embed() → 每条 unit.content 的向量
           · 构建实体索引：entity.text → [unit_ids]
                │
                ▼
  Phase 2  候选关系生成（L1/L2 快速通道）
           · L1 corefers 候选：同名/同类型实体跨 unit → 共指候选
           · L1 similar_to 候选：向量 cosine > threshold → 相似候选
           · L1 关键词 Jaccard 补充 similar_to 候选
                │
                ▼
  Phase 3  关系验证与深度发现（L3 LLM 通道）
           · score ≥ max_auto_confirm → 直接确认
           · score < min_auto_confirm → 直接拒绝
           · score 在 min_auto_confirm ~ max_auto_confirm → LLM 验证
           · L3 深度发现：对有共同实体或高相似度的 pair 发现
             caused_by/refers_to/follows_from/contradicts
                │
                ▼
  Phase 4  关系产出
           · verified candidate → Relation
           · metadata 携带证据片段和发现来源（discovery_layer: L1/L2/L3）

六类关系：
  caused_by    因果（有向）   — Phase 3 LLM
  refers_to    引用（有向）   — Phase 3 LLM
  corefers     共指（无向）   — Phase 1 FeatureExtractor + Phase 2
  follows_from 推导（有向）   — Phase 3 LLM
  similar_to   相似（无向）   — Phase 1 Embedder 向量相似度
  contradicts  矛盾（无向）   — Phase 3 LLM

纯函数：不落盘、不标记、不更新 unit。幂等性依赖 LLM temperature=0。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import Enum

from jiuwen_memory.common.embedder.base import Embedder, EmbedderProducer
from jiuwen_memory.common.feature_extractor.base import FeatureExtractor, FeatureExtractorProducer
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import FeatureSet, MemoryUnit, Relation
from jiuwen_memory.construction.associator import AssociatorProducer

from ..associator import Associator
from ..base import OperatorType

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 内部类型（实现层，不暴露到 __init__.py）
# ---------------------------------------------------------------------------


class RelationType(str, Enum):
    """关联类型（与设计文档 §3.3.2 一致）。"""

    CAUSED_BY = "caused_by"
    REFERS_TO = "refers_to"
    COREFERS = "corefers"
    FOLLOWS_FROM = "follows_from"
    SIMILAR_TO = "similar_to"
    CONTRADICTS = "contradicts"


class DiscoveryLayer(str, Enum):
    """发现层级标记。"""

    L1 = "L1"  # FeatureExtractor 实体匹配 / Embedder 向量相似度
    L2 = "L2"  # 关键词 Jaccard 补充 / 共指消解
    L3 = "L3"  # LLM 验证 / 深度发现


@dataclass
class RelationCandidate:
    """候选关系（中间结构，Phase 2→3→4 流转）。"""

    source_id: str = ""
    target_id: str = ""
    relation: RelationType = RelationType.COREFERS
    score: float = 0.0
    evidence: list[str] = field(default_factory=list)
    discovery_layer: DiscoveryLayer = DiscoveryLayer.L1
    verified: bool = False
    final_score: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LLM prompt — L3 验证与深度发现
# ---------------------------------------------------------------------------

_VERIFY_SYSTEM_PROMPT = """\
You are a relation verification engine. Given candidate relations between memory units,
determine whether each relation is valid, and adjust the confidence score.

Output ONLY a JSON array. No explanation, no markdown fences.

For each candidate:
- "valid": true if the relation genuinely holds between the two units, false otherwise.
- "adjusted_score": your confidence (0.0~1.0) in the relation's validity.
  If valid=false, set to 0.0.
- "reason": brief justification (one sentence).

Output schema:
[{
  "source_id": "id of source unit",
  "target_id": "id of target unit",
  "relation": "caused_by|refers_to|corefers|follows_from|similar_to|contradicts",
  "valid": true|false,
  "adjusted_score": 0.0~1.0,
  "reason": "brief justification"
}]
"""

_DEEP_DISCOVERY_SYSTEM_PROMPT = """\
You are a deep relation discovery engine. Given pairs of memory units that share entities
or have high similarity, discover causal, reference, derivation, and contradiction relations.

Output ONLY a JSON array. No explanation, no markdown fences.

Focus on these directed/undirected relation types:
- "caused_by": one unit describes a cause, the other describes its effect
- "refers_to": one unit explicitly references or cites the other
- "follows_from": one unit logically derives or follows from the other
- "contradicts": the two units contain contradictory information

Rules:
- Only output relations that are genuinely present.
- "confidence": 1.0 = directly evident, 0.7 = strongly implied, 0.5 = weakly implied.
  Do not output below 0.5.
- "evidence": key phrases from source units that support the relation.
- If no meaningful relation exists, return [].

Output schema:
[{
  "source_id": "id of source unit",
  "target_id": "id of target unit",
  "relation": "caused_by|refers_to|follows_from|contradicts",
  "confidence": 0.5~1.0,
  "evidence": "supporting key phrases"
}]
"""

_PAIR_CONTEXT_TEMPLATE = """\
---
[Unit: {unit_id}]
{unit_content}
---
"""


# ---------------------------------------------------------------------------
# LLMAssociator
# ---------------------------------------------------------------------------


class LLMAssociator(Associator):
    """M2 Associator：三层发现流水线，L1/L2 快速通道 + L3 LLM 验证/深度发现。"""

    def __init__(
        self,
        llm: LLM,
        feature_extractor: FeatureExtractor,
        embedder: Embedder,
        # L1/L2 阈值
        similarity_threshold: float = 0.7,
        keyword_jaccard_threshold: float = 0.3,
        entity_match_threshold: float = 0.8,
        # L3 验证阈值
        min_auto_confirm: float = 0.5,
        max_auto_confirm: float = 0.85,
        min_final_score: float = 0.5,
        # L3 深度发现
        deep_discovery: bool = True,
        max_pairs_per_llm_call: int = 10,
        # 性能
        ann_threshold: int = 50,
        max_units_per_associate: int = 200,
        # LLM 重试
        retry_max_retries: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        self._llm = llm
        self._feature_extractor = feature_extractor
        self._embedder = embedder
        self._similarity_threshold = similarity_threshold
        self._keyword_jaccard_threshold = keyword_jaccard_threshold
        self._entity_match_threshold = entity_match_threshold
        self._min_auto_confirm = min_auto_confirm
        self._max_auto_confirm = max_auto_confirm
        self._min_final_score = min_final_score
        self._deep_discovery = deep_discovery
        self._max_pairs_per_llm_call = max_pairs_per_llm_call
        self._ann_threshold = ann_threshold
        self._max_units_per_associate = max_units_per_associate
        self._retry_max_retries = retry_max_retries
        self._retry_backoff_ms = retry_backoff_ms

    def operator_type(self) -> OperatorType:
        return OperatorType.ASSOCIATOR

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

    def associate(self, units: list[MemoryUnit]) -> list[Relation]:
        """在一批记忆单元间做关联分析，返回发现的关联关系。"""
        logger.info("Associator: received %d units", len(units))
        for u in units:
            logger.info(
                "Associator: input unit id=%s tier=%s provenance=%s content=%s",
                u.id[:8],
                u.tier.value,
                u.provenance,
                u.content[:200],
            )

        if len(units) < 2:
            return []

        # 限制输入量
        if len(units) > self._max_units_per_associate:
            logger.warning(
                "Associator: %d units exceeds max_units_per_associate=%d, truncating",
                len(units),
                self._max_units_per_associate,
            )
            units = units[: self._max_units_per_associate]

        # Phase 1: 实体抽取 + 向量化
        entity_index, features_map, vectors = self._phase1_extract(units)
        logger.info(
            "Associator: Phase 1 complete — entity_index=%d keys, "
            "features_map=%d units, vectors=%d units",
            len(entity_index),
            len(features_map),
            len(vectors),
        )
        for ekey, uid_list in entity_index.items():
            logger.info("Associator: entity %s → units=%s", ekey, [uid[:8] for uid in uid_list])

        # Phase 2: 候选关系生成
        candidates = self._phase2_generate_candidates(units, entity_index, features_map, vectors)
        logger.info("Associator: Phase 2 generated %d candidate relations", len(candidates))

        if not candidates:
            return []

        # Phase 3: 关系验证与深度发现
        verified = self._phase3_verify_and_discover(units, candidates)
        logger.info("Associator: Phase 3 verified %d relations", len(verified))

        # Phase 4: 关系产出
        relations = self._phase4_produce_relations(verified)
        logger.info("Associator: Phase 4 produced %d final relations", len(relations))
        for r in relations:
            logger.info(
                "Associator: relation %s→%s type=%s score=%.2f metadata=%s",
                r.source_id[:8],
                r.target_id[:8],
                r.relation,
                r.score,
                {k: v[:50] if isinstance(v, str) else v for k, v in r.metadata.items()},
            )
        return relations

    # ------------------------------------------------------------------
    # Phase 1: 实体抽取 + 向量化
    # ------------------------------------------------------------------

    def _phase1_extract(
        self, units: list[MemoryUnit]
    ) -> tuple[dict[str, list[str]], dict[str, FeatureSet], dict[str, list[float]]]:
        """FeatureExtractor 实体抽取 + Embedder 向量化 + 构建实体索引。

        Returns:
            entity_index: entity.text → [unit_ids]（同实体跨 unit 共现映射）
            features_map: unit_id → FeatureSet（关键词 + 实体）
            vectors: unit_id → embedding vector
        """
        # 特征抽取
        texts = [u.content for u in units if u.content.strip()]
        features_map: dict[str, FeatureSet] = {}
        try:
            features_list = self._feature_extractor.extract_batch(texts)
            # 对齐 unit_id
            idx = 0
            for u in units:
                if u.content.strip():
                    if idx < len(features_list):
                        features_map[u.id] = features_list[idx]
                    idx += 1
        except Exception:
            logger.warning("Associator: FeatureExtractor unavailable, skipping entity extraction")

        # 向量化
        vectors: dict[str, list[float]] = {}
        try:
            embeddings = self._embedder.embed(texts)
            idx = 0
            for u in units:
                if u.content.strip():
                    if idx < len(embeddings):
                        vectors[u.id] = embeddings[idx]
                    idx += 1
        except Exception:
            logger.warning("Associator: Embedder unavailable, skipping vector similarity")

        # 构建实体索引：entity.text → [unit_ids]
        entity_index: dict[str, list[str]] = {}
        for u in units:
            fs = features_map.get(u.id)
            if fs is None:
                continue
            for entity in fs.entities:
                # 按文本+类型做共指候选（同名同类型大概率共指）
                key = f"{entity.text}#{entity.type}" if entity.type else entity.text
                if entity.score >= self._entity_match_threshold:
                    entity_index.setdefault(key, []).append(u.id)

        return entity_index, features_map, vectors

    # ------------------------------------------------------------------
    # Phase 2: 候选关系生成
    # ------------------------------------------------------------------

    def _phase2_generate_candidates(
        self,
        units: list[MemoryUnit],
        entity_index: dict[str, list[str]],
        features_map: dict[str, FeatureSet],
        vectors: dict[str, list[float]],
    ) -> list[RelationCandidate]:
        """L1/L2 快速通道：corefers + similar_to + 关键词 Jaccard 补充。"""

        candidates: list[RelationCandidate] = []

        # --- L1 corefers 候选 ---
        for entity_key, uid_list in entity_index.items():
            if len(uid_list) < 2:
                continue
            # 同实体跨 unit → 共指候选（无向，score 取实体置信度均值）
            for index, source_id in enumerate(uid_list):
                remaining_ids = uid_list[index + 1:]
                for target_id in remaining_ids:
                    # 计算该实体在两个 unit 的置信度均值作为候选 score
                    fs_a = features_map.get(source_id)
                    fs_b = features_map.get(target_id)
                    scores_a = [
                        e.score
                        for e in (fs_a.entities if fs_a else [])
                        if (f"{e.text}#{e.type}" if e.type else e.text) == entity_key
                    ]
                    scores_b = [
                        e.score
                        for e in (fs_b.entities if fs_b else [])
                        if (f"{e.text}#{e.type}" if e.type else e.text) == entity_key
                    ]
                    avg_score = (
                        sum(scores_a) / max(len(scores_a), 1)
                        + sum(scores_b) / max(len(scores_b), 1)
                    ) / 2.0
                    candidates.append(
                        RelationCandidate(
                            source_id=source_id,
                            target_id=target_id,
                            relation=RelationType.COREFERS,
                            score=avg_score,
                            evidence=[f"shared entity: {entity_key}"],
                            discovery_layer=DiscoveryLayer.L1,
                        )
                    )

        # --- L1 similar_to 候选：向量 cosine ---
        if vectors and len(vectors) >= 2:
            vid_list = list(vectors.keys())
            for index, a_id in enumerate(vid_list):
                remaining_ids = vid_list[index + 1:]
                for b_id in remaining_ids:
                    cosine = self._cosine_similarity(vectors[a_id], vectors[b_id])
                    if cosine >= self._similarity_threshold:
                        candidates.append(
                            RelationCandidate(
                                source_id=a_id,
                                target_id=b_id,
                                relation=RelationType.SIMILAR_TO,
                                score=cosine,
                                evidence=[f"cosine={cosine:.3f}"],
                                discovery_layer=DiscoveryLayer.L1,
                            )
                        )

        # --- L2 关键词 Jaccard 补充 similar_to ---
        if features_map and len(features_map) >= 2:
            fid_list = list(features_map.keys())
            for index, a_id in enumerate(fid_list):
                remaining_ids = fid_list[index + 1:]
                for b_id in remaining_ids:
                    # 跳过已通过 cosine 发现的 pair（避免重复）
                    existing = any(
                        c.source_id == a_id
                        and c.target_id == b_id
                        and c.relation == RelationType.SIMILAR_TO
                        for c in candidates
                    )
                    if existing:
                        continue
                    kw_a = set(features_map[a_id].keywords)
                    kw_b = set(features_map[b_id].keywords)
                    if not kw_a or not kw_b:
                        continue
                    jaccard = len(kw_a & kw_b) / len(kw_a | kw_b)
                    if jaccard >= self._keyword_jaccard_threshold:
                        shared_kw = sorted(kw_a & kw_b)
                        candidates.append(
                            RelationCandidate(
                                source_id=a_id,
                                target_id=b_id,
                                relation=RelationType.SIMILAR_TO,
                                score=jaccard,
                                evidence=[f"shared keywords: {','.join(shared_kw)}"],
                                discovery_layer=DiscoveryLayer.L2,
                            )
                        )

        return candidates

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算两个向量的 cosine similarity。"""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    # ------------------------------------------------------------------
    # Phase 3: 关系验证与深度发现
    # ------------------------------------------------------------------

    def _phase3_verify_and_discover(
        self,
        units: list[MemoryUnit],
        candidates: list[RelationCandidate],
    ) -> list[RelationCandidate]:
        """L3 LLM 通道：验证候选项 + 深度发现新关系。"""

        unit_map = {u.id: u for u in units}
        verified: list[RelationCandidate] = []

        # --- 3a: 按阈值分流候选项 ---
        auto_confirmed: list[RelationCandidate] = []
        to_verify: list[RelationCandidate] = []
        rejected: list[RelationCandidate] = []

        for c in candidates:
            if c.score >= self._max_auto_confirm:
                # 高置信度：直接确认
                c.verified = True
                c.final_score = c.score
                auto_confirmed.append(c)
            elif c.score >= self._min_auto_confirm:
                # 中置信度：需要 LLM 验证
                to_verify.append(c)
            else:
                # 低置信度：直接拒绝
                rejected.append(c)

        logger.info(
            "Associator: Phase 3分流 — auto_confirmed=%d, to_verify=%d, rejected=%d",
            len(auto_confirmed),
            len(to_verify),
            len(rejected),
        )

        # auto_confirmed 直接保留
        verified.extend(auto_confirmed)

        # --- 3b: LLM 验证候选项 ---
        if to_verify:
            try:
                verified_batch = self._llm_verify_candidates(unit_map, to_verify)
                verified.extend(verified_batch)
            except Exception:
                logger.warning(
                    "Associator: LLM verification failed, keeping unverified candidates as-is"
                )
                # 降级：保留未验证但 score >= min_auto_confirm 的候选项
                for c in to_verify:
                    c.verified = True  # 标记为「未 LLM 验证但保留」
                    c.final_score = c.score
                    c.metadata["llm_verified"] = "false"
                    verified.append(c)

        # --- 3c: LLM 深度发现 ---
        deep_candidates: list[RelationCandidate] = []
        if self._deep_discovery:
            try:
                deep_candidates = self._llm_deep_discover(unit_map, verified)
                verified.extend(deep_candidates)
            except Exception:
                logger.warning("Associator: LLM deep discovery failed, skipping")

        # 过滤 final_score < min_final_score
        verified = [c for c in verified if c.final_score >= self._min_final_score]

        return verified

    def _llm_verify_candidates(
        self,
        unit_map: dict[str, MemoryUnit],
        candidates: list[RelationCandidate],
    ) -> list[RelationCandidate]:
        """LLM 验证候选项：分批构建 prompt → 调 LLM → 解析 → 更新 verified/final_score。"""

        verified: list[RelationCandidate] = []

        # 分批：每批 max_pairs_per_llm_call 个候选
        batches = []
        for i in range(0, len(candidates), self._max_pairs_per_llm_call):
            batch_end = i + self._max_pairs_per_llm_call
            batches.append(candidates[i:batch_end])

        for batch in batches:
            try:
                batch_verified = self._verify_one_batch(unit_map, batch)
                verified.extend(batch_verified)
            except Exception:
                logger.warning(
                    "Associator: LLM verify batch failed (%d candidates), skipping", len(batch)
                )
                # 失败隔离：该 batch 降级保留
                for c in batch:
                    c.verified = True
                    c.final_score = c.score
                    c.metadata["llm_verified"] = "false"
                    verified.append(c)

        return verified

    def _verify_one_batch(
        self,
        unit_map: dict[str, MemoryUnit],
        candidates: list[RelationCandidate],
    ) -> list[RelationCandidate]:
        """单批 LLM 验证。"""

        # 构建 context：涉及的 unit 全文
        involved_ids = set()
        for c in candidates:
            involved_ids.add(c.source_id)
            involved_ids.add(c.target_id)

        context_parts = []
        for uid in sorted(involved_ids):
            u = unit_map.get(uid)
            if u:
                context_parts.append(
                    _PAIR_CONTEXT_TEMPLATE.format(unit_id=u.id, unit_content=u.content)
                )
        unit_context = "\n".join(context_parts)

        # 构建候选列表文本
        candidate_lines = []
        for c in candidates:
            candidate_lines.append(
                f"- source_id={c.source_id}, target_id={c.target_id}, "
                f"relation={c.relation.value}, current_score={c.score:.2f}"
            )
        candidate_text = "\n".join(candidate_lines)

        user_text = (
            f"Memory units:\n{unit_context}\n\nCandidate relations to verify:\n{candidate_text}"
        )

        from jiuwen_memory.common.type_def import ChatMessage

        messages = [
            ChatMessage(role="system", content=_VERIFY_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_text),
        ]

        response = self._call_llm_with_retry(messages)
        items = self._parse_llm_response(response)

        # 更新候选项
        verified: list[RelationCandidate] = []
        for item in items:
            # 找到匹配的 candidate
            source_id = item.get("source_id", "")
            target_id = item.get("target_id", "")
            relation_str = item.get("relation", "")

            matched = None
            for c in candidates:
                if c.source_id == source_id and c.target_id == target_id:
                    try:
                        if RelationType(relation_str) == c.relation:
                            matched = c
                            break
                    except ValueError:
                        # 关系类型不匹配，跳过
                        continue

            if matched is None:
                continue

            valid = item.get("valid", False)
            adjusted_score = float(item.get("adjusted_score", 0.0))
            reason = item.get("reason", "")

            matched.verified = bool(valid)
            matched.final_score = adjusted_score if valid else 0.0
            matched.metadata["llm_verified"] = "true"
            matched.metadata["verify_reason"] = reason
            verified.append(matched)

        # LLM 未覆盖的候选项：降级保留
        for c in candidates:
            if c not in verified:
                c.verified = True
                c.final_score = c.score
                c.metadata["llm_verified"] = "false"
                verified.append(c)

        return verified

    def _llm_deep_discover(
        self,
        unit_map: dict[str, MemoryUnit],
        verified_candidates: list[RelationCandidate],
    ) -> list[RelationCandidate]:
        """L3 深度发现：对已确认关联的 pair 发现 caused_by/refers_to/follows_from/contradicts。"""

        # 筛选有共同实体或高相似度的 pair（已有的 corefers/similar_to 是 L3 深度发现的种子）
        seed_pairs = [
            c
            for c in verified_candidates
            if c.verified and c.relation in (RelationType.COREFERS, RelationType.SIMILAR_TO)
        ]

        if not seed_pairs:
            return []

        # 分批
        batches = []
        for i in range(0, len(seed_pairs), self._max_pairs_per_llm_call):
            batch_end = i + self._max_pairs_per_llm_call
            batches.append(seed_pairs[i:batch_end])

        deep_candidates: list[RelationCandidate] = []

        for batch in batches:
            try:
                batch_result = self._deep_discover_one_batch(unit_map, batch)
                deep_candidates.extend(batch_result)
            except Exception:
                logger.warning("Associator: LLM deep discovery batch failed, skipping")
                continue

        return deep_candidates

    def _deep_discover_one_batch(
        self,
        unit_map: dict[str, MemoryUnit],
        seed_pairs: list[RelationCandidate],
    ) -> list[RelationCandidate]:
        """单批 LLM 深度发现。"""

        # 构建 context：涉及的 unit 全文
        involved_ids = set()
        for c in seed_pairs:
            involved_ids.add(c.source_id)
            involved_ids.add(c.target_id)

        context_parts = []
        for uid in sorted(involved_ids):
            u = unit_map.get(uid)
            if u:
                context_parts.append(
                    _PAIR_CONTEXT_TEMPLATE.format(unit_id=u.id, unit_content=u.content)
                )
        unit_context = "\n".join(context_parts)

        # 构建 pair 列表
        pair_lines = []
        for c in seed_pairs:
            pair_lines.append(
                f"- Pair: ({c.source_id}, {c.target_id}), "
                f"existing relation: {c.relation.value}, score: {c.final_score:.2f}"
            )
        pair_text = "\n".join(pair_lines)

        user_text = f"Memory units:\n{unit_context}\n\nPairs with existing relations:\n{pair_text}"

        from jiuwen_memory.common.type_def import ChatMessage

        messages = [
            ChatMessage(role="system", content=_DEEP_DISCOVERY_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_text),
        ]

        response = self._call_llm_with_retry(messages)
        items = self._parse_llm_response(response)

        # 构建 RelationCandidate
        candidates: list[RelationCandidate] = []
        for item in items:
            source_id = item.get("source_id", "")
            target_id = item.get("target_id", "")
            relation_str = item.get("relation", "")
            confidence = float(item.get("confidence", 0.0))
            evidence = item.get("evidence", "")

            # 验证 source/target 在已知 unit 中
            if source_id not in unit_map or target_id not in unit_map:
                continue

            try:
                relation_type = RelationType(relation_str)
            except ValueError:
                continue

            # 只接受 L3 独有的关系类型
            if relation_type not in (
                RelationType.CAUSED_BY,
                RelationType.REFERS_TO,
                RelationType.FOLLOWS_FROM,
                RelationType.CONTRADICTS,
            ):
                continue

            candidates.append(
                RelationCandidate(
                    source_id=source_id,
                    target_id=target_id,
                    relation=relation_type,
                    score=confidence,
                    evidence=[evidence] if evidence else [],
                    discovery_layer=DiscoveryLayer.L3,
                    verified=True,
                    final_score=confidence,
                    metadata={"llm_verified": "true", "discovery_method": "deep"},
                )
            )

        return candidates

    # ------------------------------------------------------------------
    # LLM 调用与解析（与 ExtractorImpl/LLMAbstractor 一致）
    # ------------------------------------------------------------------

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
                        "Associator: LLM call failed (attempt %d), retrying in %.1fs",
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
            logger.debug("Associator: direct JSON parse failed, trying stripped JSON")

        # 解析失败：尝试提取 JSON 部分（去除 markdown fences 等噪声）
        cleaned = self._strip_non_json(response)
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            logger.warning("Associator: LLM response not valid JSON, returning empty")
            return []
        return []

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

    # ------------------------------------------------------------------
    # Phase 4: 关系产出
    # ------------------------------------------------------------------

    def _phase4_produce_relations(self, verified: list[RelationCandidate]) -> list[Relation]:
        """将验证后的 RelationCandidate 转为 Relation。"""

        relations: list[Relation] = []
        for c in verified:
            if not c.verified:
                continue
            if c.final_score < self._min_final_score:
                continue

            metadata: dict[str, str] = {
                "discovery_layer": c.discovery_layer.value,
                "evidence": "; ".join(c.evidence) if c.evidence else "",
            }
            metadata.update(c.metadata)

            relations.append(
                Relation(
                    source_id=c.source_id,
                    target_id=c.target_id,
                    relation=c.relation.value,
                    score=c.final_score,
                    metadata=metadata,
                )
            )

        # 无向关系去重（corefers/similar_to/contradicts：A-B 与 B-A 等价）
        seen: set[tuple[str, str, str]] = set()
        deduped: list[Relation] = []
        undirected = {
            RelationType.COREFERS.value,
            RelationType.SIMILAR_TO.value,
            RelationType.CONTRADICTS.value,
        }

        for r in relations:
            if r.relation in undirected:
                # 无向：排序 id 确保 A-B 与 B-A 不重复
                key = (min(r.source_id, r.target_id), max(r.source_id, r.target_id), r.relation)
            else:
                key = (r.source_id, r.target_id, r.relation)
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        return deduped


# -- 注册到 AssociatorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@AssociatorProducer.register("llm")
def _build(config):
    return LLMAssociator(
        llm=LlmProducer.dep(config, default="echo"),
        feature_extractor=FeatureExtractorProducer.dep(config, default="keyword"),
        embedder=EmbedderProducer.dep(config, default="hashing"),
        similarity_threshold=config.get("associator_similarity_threshold", 0.7),
        keyword_jaccard_threshold=config.get("associator_keyword_jaccard_threshold", 0.3),
        entity_match_threshold=config.get("associator_entity_match_threshold", 0.8),
        min_auto_confirm=config.get("associator_min_auto_confirm", 0.5),
        max_auto_confirm=config.get("associator_max_auto_confirm", 0.85),
        min_final_score=config.get("associator_min_final_score", 0.5),
        deep_discovery=config.get("associator_deep_discovery", True),
        max_pairs_per_llm_call=config.get("associator_max_pairs_per_llm_call", 10),
        ann_threshold=config.get("associator_ann_threshold", 50),
        max_units_per_associate=config.get("associator_max_units_per_associate", 200),
        retry_max_retries=config.get("associator_retry_max", 3),
        retry_backoff_ms=config.get("associator_retry_backoff", 1000),
    )
