# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""向量去重召回：Embedder → VectorStore.search → 加载 unit → 聚合。

召回命中 id 为 chunk 复合 id（``{unit_id}-{chunk_id}``），按记录 metadata 的
``unit_id`` 归并到 unit 粒度（同 unit 多 chunk 取 MaxP），再按 cosine 阈值过滤。
向量开关关闭时装配不选本路（改用 KeywordDedup），但本实现自身不感知。
"""

from __future__ import annotations

from jiuwen_memory.common.embedder.base import Embedder, EmbedderProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import FilterClause, FilterOp, LifecycleState, MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.dedup import (
    Dedup,
    DedupProducer,
    dedup_project_filter,
    is_project_isolated_dedup,
    merge_dedup_hits,
    same_scope,
)
from jiuwen_memory.storage.store_manager import (
    StoreManager,
    StoreManagerProducer,
    resolve_name,
)
from jiuwen_memory.storage.types import VectorQuery

logger = get_logger(__name__)


def _unit_id_from_record_id(record_id: str) -> str:
    """从向量索引 record_id (格式 ``{unit_id}-{chunk_id}``) 中提取 unit_id。

    VectorIndexBuilder 使用 f"{unit.id}-{chunk.id}" 格式，chunk.id 为整数 seq。
    取最后一个 "-" 前的部分作为 unit_id。
    """
    parts = record_id.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else record_id


class VectorDedup(Dedup):
    """向量语义去重召回路（包一个 VectorStore + Embedder）。"""

    def __init__(
        self,
        storage: StoreManager,
        embedder: Embedder,
        *,
        vector_name: str = "default",
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
        self._vector_store = storage.vector(vector_name)
        self._embedder = embedder

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER  # 去重召回服务于 evolver，复用其算子类型

    def health(self) -> None:
        return None

    def recall(self, candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]:
        # Step A: 向量化候选
        try:
            candidate_vector = self._embedder.embed([candidate.content])[0]
        except Exception as exc:
            logger.warning(
                "VectorDedup: Embedder failed for %s, recall empty: %s",
                candidate.id[:8], exc,
            )
            return []

        # Step B: 召回已有相似记忆
        base_filters: list[FilterClause] = []
        if self._tier_filter:
            base_filters.append(
                FilterClause(field="tier", op=FilterOp.EQ, value=candidate.tier.value)
            )
        project_filter = dedup_project_filter(candidate)
        filters = [*base_filters, project_filter] if project_filter is not None else base_filters

        hit_units = self._search_and_aggregate(candidate, candidate_vector, filters)
        # 非文档向量索引不会给缺 project 键的历史全局记录补 project=""；
        # project == "" 时合并无 project 过滤查询结果（按 unit.id 取 max 并截断 top_k），
        # 避免新旧全局记录混存时漏掉高分旧记录（仍经 is_project_isolated_dedup 过滤）。
        if project_filter is not None and project_filter.value == "":
            fallback_hits = self._search_and_aggregate(candidate, candidate_vector, base_filters)
            hit_units = merge_dedup_hits(hit_units, fallback_hits, top_k=self._top_k)
        return hit_units

    def _search_and_aggregate(
        self,
        candidate: MemoryUnit,
        candidate_vector: list[float],
        filters: list[FilterClause],
    ) -> list[tuple[MemoryUnit, float]]:
        query = VectorQuery(vector=candidate_vector, top_k=self._top_k, filters=filters)

        scope = candidate.scope
        try:
            hits = self._vector_store.search(scope, query)
        except Exception as exc:
            logger.warning(
                "VectorDedup: VectorStore.search failed for %s, recall empty: %s",
                candidate.id[:8], exc,
            )
            return []

        logger.info(
            "[trace/dedup] vector_recall | candidate_id=%s | vector_store=%s "
            "| scope=%r | raw_hits=%d",
            candidate.id[:8],
            type(self._vector_store).__name__,
            scope,
            len(hits),
        )

        # 过滤掉候选自身的向量记录（unit.id-{chunk.id} 格式）
        hits = [h for h in hits if not h.id.startswith(candidate.id + "-")]
        if not hits:
            return []

        # 加载 unit → dict 聚合取 MaxP（O(1) 查找/更新，替代旧 O(n²) 列表扫描）
        aggregated: dict[str, tuple[MemoryUnit, float]] = {}
        for scored_id in hits:
            if scored_id.score < self._min_similarity:
                continue
            unit_id = _unit_id_from_record_id(scored_id.id)
            unit = self._load_unit(unit_id, scope)
            if unit is None or unit.lifecycle != LifecycleState.ACTIVE:
                continue
            # scope_filter：只保留与候选同 scope 的 unit
            if self._scope_filter and not same_scope(unit.scope, candidate.scope):
                continue
            if is_project_isolated_dedup(candidate, unit):
                continue
            # 跳过候选自身
            if unit.id == candidate.id:
                continue
            # 跳过中期记忆原文——派生必然与源原文语义接近，让原文参与对照会触发
            # LLM dedup 判 NOOP 丢派生。Engine.write middle=true 时给原文打
            # metadata.middle=true 标记，dedup 按此过滤。
            if unit.system_metadata.get("middle") == "true":
                continue
            # dict MaxP 聚合
            if unit.id not in aggregated or scored_id.score > aggregated[unit.id][1]:
                aggregated[unit.id] = (unit, scored_id.score)

        return sorted(aggregated.values(), key=lambda x: x[1], reverse=True)


# -- 注册到 DedupProducer（实现自注册，新增无需改 producer/build_kernel） -------- #




@DedupProducer.register("vector")
def _build(config):
    return VectorDedup(
        storage=StoreManagerProducer.resolve(config),
        embedder=EmbedderProducer.dep(config, default="hashing"),
        vector_name=resolve_name(config, "vector_store"),
        kv_name=resolve_name(config, "kv_store"),
        min_similarity=config.get("dedup_min_similarity", 0.5),
        top_k=config.get("dedup_top_k", 5),
        tier_filter=config.get("dedup_tier_filter", False),
        scope_filter=config.get("dedup_scope_filter", True),
    )
