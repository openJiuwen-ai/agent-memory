# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""倒排去重召回：FulltextStore.search → 加载 unit → Jaccard 相似度过滤聚合。

只配倒排索引（``vector_enabled=False``）时装配选本路——VectorStore 恒空会使
向量去重失效。FulltextStore 后端相关性分只决定候选池排序；``KeywordDedup``
对加载后的记忆内容计算 token-set Jaccard，供 Evolver 的相似度阈值使用。

FulltextStore 按 unit 建索引（Document.id = unit.id），故召回命中 id 直接是
unit_id，无需解析 chunk 复合 id。``InMemoryFulltextStore.search`` 不消费
``filters``，tier 过滤在加载 unit 后做。
"""

from __future__ import annotations

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.tokenizer import Tokenizer
from jiuwen_memory.common.tokenizer.base import TokenizerProducer
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.dedup import Dedup, DedupProducer, same_scope
from jiuwen_memory.storage.store_manager import (
    StoreManager,
    StoreManagerProducer,
    resolve_name,
)
from jiuwen_memory.storage.types import TextQuery

logger = get_logger(__name__)


class KeywordDedup(Dedup):
    """倒排/关键词去重召回路（FulltextStore 召回 + Jaccard 相似度）。"""

    def __init__(
        self,
        storage: StoreManager,
        tokenizer: Tokenizer,
        *,
        fulltext_name: str = "default",
        kv_name: str = "default",
        min_similarity: float = 0.5,
        top_k: int = 5,
        tier_filter: bool = True,
        scope_filter: bool = True,
    ) -> None:
        super().__init__(
            storage.kv(kv_name),
            min_similarity=min_similarity,
            top_k=top_k,
            tier_filter=tier_filter,
            scope_filter=scope_filter,
        )
        self._fulltext = storage.fulltext(fulltext_name)
        self._tokenizer = tokenizer

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def recall(self, candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]:
        # FulltextStore 只负责召回候选；后端相关性分不进入相似度阈值。
        query = TextQuery(text=candidate.content, top_k=self._top_k)
        scope = candidate.scope
        try:
            hits = self._fulltext.search(scope, query)
        except Exception as exc:
            logger.warning(
                "KeywordDedup: FulltextStore.search failed for %s, recall empty: %s",
                candidate.id[:8], exc,
            )
            return []

        # 过滤候选自身（FulltextStore 按 unit 建索引，doc_id == unit.id）
        hits = [h for h in hits if h.id != candidate.id]
        if not hits:
            return []

        try:
            candidate_tokens = set(self._tokenizer.tokenize(candidate.content))
        except Exception as exc:
            logger.warning(
                "KeywordDedup: tokenizer failed for candidate %s, recall empty: %s",
                candidate.id[:8], exc,
            )
            return []

        # 加载 unit → 计算 Jaccard → dict 聚合取 MaxP。
        aggregated: dict[str, tuple[MemoryUnit, float]] = {}
        for scored_id in hits:
            unit = self._load_unit(scored_id.id, scope)
            if unit is None or unit.lifecycle != LifecycleState.ACTIVE:
                continue
            # tier_filter: 可选按 tier 过滤（默认 False，允许跨层去重）
            if self._tier_filter and unit.tier != candidate.tier:
                continue
            # scope_filter: 只保留与候选同 scope 的 unit
            if self._scope_filter and not same_scope(unit.scope, candidate.scope):
                continue
            # 跳过中期记忆原文——派生必然与源原文语义接近，让原文参与对照会
            # 触发 NOOP 丢派生。dedup 只查"派生是否与已沉淀长期记忆重复"。
            if unit.system_metadata.get("middle") == "true":
                continue
            try:
                existing_tokens = set(self._tokenizer.tokenize(unit.content))
            except Exception as exc:
                logger.warning(
                    "KeywordDedup: tokenizer failed for unit %s, skip it: %s",
                    unit.id[:8], exc,
                )
                continue

            union = candidate_tokens | existing_tokens
            similarity = len(candidate_tokens & existing_tokens) / len(union) if union else 0.0
            if similarity < self._min_similarity:
                continue
            if unit.id not in aggregated or similarity > aggregated[unit.id][1]:
                aggregated[unit.id] = (unit, similarity)

        hit_units = sorted(aggregated.values(), key=lambda x: x[1], reverse=True)
        return hit_units


# -- 注册到 DedupProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@DedupProducer.register("keyword")
def _build(config):
    return KeywordDedup(
        storage=StoreManagerProducer.resolve(config),
        tokenizer=TokenizerProducer.dep(config, default="whitespace"),
        fulltext_name=resolve_name(config, "fulltext_store"),
        kv_name=resolve_name(config, "kv_store"),
        min_similarity=config.get("dedup_min_similarity", 0.5),
        top_k=config.get("dedup_top_k", 5),
        tier_filter=config.get("dedup_tier_filter", False),
        scope_filter=config.get("dedup_scope_filter", True),
    )
