"""倒排去重召回：FulltextStore.search → 加载 unit → 过滤聚合。

只配倒排索引（``vector_enabled=False``）时装配选本路——VectorStore 恒空会使
向量去重失效，倒排召回用词重叠率计分（0~1，与 cosine 同量纲），阈值直接复用。

FulltextStore 按 unit 建索引（Document.id = unit.id），故召回命中 id 直接是
unit_id，无需解析 chunk 复合 id。``InMemoryFulltextStore.search`` 不消费
``filters``，tier 过滤在加载 unit 后做。
"""

from __future__ import annotations

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.dedup import Dedup, DedupProducer, same_scope
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import TextQuery

logger = get_logger(__name__)


class KeywordDedup(Dedup):
    """倒排/关键词去重召回路（包一个 FulltextStore）。"""

    def __init__(
        self,
        storage: Storage,
        *,
        min_similarity: float = 0.5,
        top_k: int = 5,
        tier_filter: bool = True,
        scope_filter: bool = True,
    ) -> None:
        super().__init__(
            storage,
            min_similarity=min_similarity,
            top_k=top_k,
            tier_filter=tier_filter,
            scope_filter=scope_filter,
        )
        self._fulltext = storage.fulltext

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def recall(self, candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]:
        # 召回已有相似记忆：用候选 content 做关键词检索
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

        # 加载 unit → dict 聚合取 MaxP（O(1) 查找/更新，替代旧 O(n²) 列表扫描）
        aggregated: dict[str, tuple[MemoryUnit, float]] = {}
        for scored_id in hits:
            if scored_id.score < self._min_similarity:
                continue
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
            if unit.metadata.get("middle") == "true":
                continue
            # dict MaxP 聚合
            if unit.id not in aggregated or scored_id.score > aggregated[unit.id][1]:
                aggregated[unit.id] = (unit, scored_id.score)

        hit_units = sorted(aggregated.values(), key=lambda x: x[1], reverse=True)
        return hit_units


# -- 注册到 DedupProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@DedupProducer.register("keyword")
def _build(config):
    return KeywordDedup(
        storage=StorageProducer.resolve(config),
        min_similarity=config.get("dedup_min_similarity", 0.5),
        top_k=config.get("dedup_top_k", 5),
        tier_filter=config.get("dedup_tier_filter", False),
        scope_filter=config.get("dedup_scope_filter", True),
    )
