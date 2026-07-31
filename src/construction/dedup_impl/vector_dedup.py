"""向量去重召回：Embedder → VectorStore.search → 加载 unit → 聚合。

召回命中 id 为 chunk 复合 id（``{unit_id}-{chunk_id}``），按记录 metadata 的
``unit_id`` 归并到 unit 粒度（同 unit 多 chunk 取 MaxP），再按 cosine 阈值过滤。
向量开关关闭时装配不选本路（改用 KeywordDedup），但本实现自身不感知。
"""

from __future__ import annotations

from typing import List, Tuple

from common.embedder.base import Embedder, EmbedderProducer
from common.log import get_logger
from common.type_def import FilterClause, FilterOp, LifecycleState, MemoryUnit
from construction.base import OperatorType
from construction.dedup import Dedup, DedupProducer, same_scope
from storage.kv import KvProducer, KVStore
from storage.types import VectorQuery
from storage.vector import VectorProducer, VectorStore

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
        vector_store: VectorStore,
        embedder: Embedder,
        kv: KVStore,
        *,
        min_similarity: float = 0.5,
        top_k: int = 5,
        tier_filter: bool = True,
        scope_filter: bool = True,
    ) -> None:
        super().__init__(
            kv,
            min_similarity=min_similarity,
            top_k=top_k,
            tier_filter=tier_filter,
            scope_filter=scope_filter,
        )
        self._vector_store = vector_store
        self._embedder = embedder

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER  # 去重召回服务于 evolver，复用其算子类型

    def health(self) -> None:
        return None

    def recall(self, candidate: MemoryUnit) -> List[Tuple[MemoryUnit, float]]:
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
        filters: list[FilterClause] = []
        if self._tier_filter:
            filters.append(FilterClause(field="tier", op=FilterOp.EQ, value=candidate.tier.value))

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

        # 过滤掉候选自身的向量记录（unit.id-{chunk.id} 格式）
        hits = [h for h in hits if not h.id.startswith(candidate.id + "-")]
        if not hits:
            return []

        # 加载 unit → dict 聚合取 MaxP（O(1) 查找/更新，替代旧 O(n²) 列表扫描）
        aggregated: dict[str, Tuple[MemoryUnit, float]] = {}
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
            # 跳过候选自身
            if unit.id == candidate.id:
                continue
            # 跳过中期记忆原文——派生必然与源原文语义接近，让原文参与对照会触发
            # LLM dedup 判 NOOP 丢派生。Engine.write middle=true 时给原文打
            # metadata.middle=true 标记，dedup 按此过滤。
            if unit.metadata.get("middle") == "true":
                continue
            # dict MaxP 聚合
            if unit.id not in aggregated or scored_id.score > aggregated[unit.id][1]:
                aggregated[unit.id] = (unit, scored_id.score)

        hit_units = sorted(aggregated.values(), key=lambda x: x[1], reverse=True)
        return hit_units


# -- 注册到 DedupProducer（实现自注册，新增无需改 producer/build_kernel） -------- #




@DedupProducer.register("vector")
def _build(config):
    return VectorDedup(
        vector_store=VectorProducer.dep(config, default="memory"),
        embedder=EmbedderProducer.dep(config, default="hashing"),
        kv=KvProducer.dep(config, default="memory"),
        min_similarity=config.get("dedup_min_similarity", 0.5),
        top_k=config.get("dedup_top_k", 5),
        tier_filter=config.get("dedup_tier_filter", False),
        scope_filter=config.get("dedup_scope_filter", True),
    )
