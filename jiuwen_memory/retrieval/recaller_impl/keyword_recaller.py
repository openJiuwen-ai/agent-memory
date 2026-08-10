"""最小实现：:class:`~retrieval.recaller.Recaller` 的 KEYWORD 通道。

消费 ``ParsedQuery``，组装 :class:`~storage.types.TextQuery`（scope 落查询的专用
scope 入参做原生隔离、``scalar_filters`` 落 filters 做硬过滤），经注入的
:class:`~storage.fulltext.FulltextStore` 召回，再按记录 ``metadata['unit_id']`` 归并到
unit 粒度（与向量通道同一套口径，全文按 unit 建索引时为恒等映射）。多路并行与合并由
Retriever/Fuser 负责，本通道不感知其他通道。

支持 L0/L1 分层召回：``layer`` 参数（默认 "l2"=content 全文；"l0"/"l1"=预生成
概要/片段分表倒排）。store 为 None 时 recall 返空（该层未注入）。
"""

from __future__ import annotations

from statistics import median

from jiuwen_memory.common.type_def.entity import (
    EntityStoreFilters,
    hash_entity_text,
)
from jiuwen_memory.common.type_def.normalizer import EntityNormalizer
from jiuwen_memory.common.type_def.scope import space_id_from_scope
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.recaller import Recaller, RecallerProducer
from jiuwen_memory.retrieval.types import ParsedQuery, RecallChannel, ScoredUnit
from jiuwen_memory.storage.entity_store import EntityStore
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import TextQuery

from .unit_aggregation import aggregate_to_units

logger = get_logger(__name__)

# batch 2（实体关联扩展）候选数硬上限，防极端发散拖垮后续 fuse/点读。
_ENTITY_EXPANSION_TOP_K = 50
# 衰减系数：关联命中越多，单次扩展贡献越小（避免一个高频实体拉回海量低分记忆）。
_ENTITY_DECAY_FACTOR = 0.001


class KeywordRecaller(Recaller):
    """关键词/全文召回路（包一个 FulltextStore，支持 content/L0/L1 分层）。"""

    def __init__(
        self,
        storage: Storage,
        *,
        layer: str = "l2",
        entity_store: EntityStore | None = None,
    ) -> None:
        port_name = "default" if layer == "l2" else f"layers_{layer}"
        self._fulltext = (
            storage.fulltext_port(port_name) if storage.has_fulltext_port(port_name) else None
        )
        self._layer = layer  # "l2"(content) | "l0" | "l1"
        self._entity_store = entity_store  # L2 实体关联扩展用；None/非 L2 时不扩展

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    @property
    def layer(self) -> str:
        """当前召回层级（l2/l0/l1），公开只读。"""
        return self._layer

    @property
    def fulltext_store(self) -> FulltextStore | None:
        """注入的全文 store（只读；None 表示该层未注入，recall 返空）。"""
        return self._fulltext

    def channel(self) -> RecallChannel:
        return RecallChannel.KEYWORD

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]:
        if self._fulltext is None:
            return []  # store 未注入（该层未配）→ 跳过
        tq = TextQuery(
            text=query.rewritten or query.raw,
            top_k=top_k,
            filters=query.scalar_filters,  # scope 不混进 filters，单独走入参
        )
        hits = self._fulltext.search(scope, tq)
        records = self._fulltext.get(scope, [h.id for h in hits])
        batch1 = aggregate_to_units(hits, records, RecallChannel.KEYWORD)

        # L2 实体关联扩展：从 batch 1 候选 metadata['entities'] 反查关联 unit。
        batch2 = self._expand_by_entities(scope, records, batch1)

        result = self._merge_maxp(batch1, batch2)
        logger.info(
            "KeywordRecaller: layer=%s scope=%s top_k=%d hits=%d units=%d batch2=%d merged=%d",
            self._layer, scope, top_k, len(hits), len(batch1), len(batch2), len(result),
        )
        return result

    # ------------------------------------------------------------------
    # L2 实体关联扩展
    # ------------------------------------------------------------------

    def _expand_by_entities(
        self,
        scope: Scope,
        records: list,
        batch1: list[ScoredUnit],
    ) -> list[ScoredUnit]:
        """从 batch 1 候选的 entities metadata 反查关联 unit，返回 batch 2 候选。

        不抽 query 实体（零 spaCy）：种子明文来自写入侧 LLM 抽取并落进 L2 文档
        metadata['entities']。打分用中位数锚定 + 衰减，保证关联记忆分不会过低
        也不会压过 batch 1 原生命中。
        """
        if self._entity_store is None or self._layer != "l2" or not batch1:
            return []

        # 收集 batch 1 候选记录里的所有 entities 明文，归一化+hash 去重。
        hashes: set[str] = set()
        for rec in records:
            meta = getattr(rec, "metadata", None) or {}
            for entity_text in meta.get("entities") or []:
                normalized = EntityNormalizer.normalize(entity_text)
                if normalized:
                    hashes.add(hash_entity_text(normalized))
        if not hashes:
            return []

        space_id = space_id_from_scope(scope)
        filters = EntityStoreFilters.from_scope(scope)
        try:
            entity_records = self._entity_store.find_by_entity_text_hash(
                space_id, tuple(hashes), filters=filters, limit=self._entity_list_limit(len(hashes)),
            )
        except Exception:
            logger.warning("entity_expansion_lookup_failed space_id=%s", space_id, exc_info=True)
            return []

        # 关联 unit_id → 命中次数（同一 unit 被多个 entity 关联，count 越多衰减越快）
        linked_counts: dict[str, int] = {}
        batch1_ids = {u.unit_id for u in batch1}
        for er in entity_records:
            for uid in er.linked_memory_ids:
                if uid in batch1_ids:
                    continue  # batch 1 已命中，不再扩展（避免重复）
                linked_counts[uid] = linked_counts.get(uid, 0) + 1
        if not linked_counts:
            return []

        # 中位数锚定打分（rank-based，与 RRF 口径一致——fuser 只看 rank 不看绝对分）
        scores = [u.score for u in batch1]
        if len(scores) >= 3:
            anchor = median(scores)
        else:
            anchor = (max(scores) if scores else 0.0) * 0.5  # batch1<3 fallback

        batch2: list[ScoredUnit] = []
        for uid, count in linked_counts.items():
            decay = 1.0 / (1.0 + _ENTITY_DECAY_FACTOR * (count - 1) ** 2)
            batch2.append(ScoredUnit(
                unit_id=uid,
                score=anchor * decay,
                channel=RecallChannel.KEYWORD,
            ))
        batch2.sort(key=lambda u: u.score, reverse=True)
        return batch2[:_ENTITY_EXPANSION_TOP_K]

    @staticmethod
    def _entity_list_limit(hash_count: int) -> int:
        """hash→实体记录的 list 上限。按 hash 数给余量，但封顶防过大。"""
        return max(hash_count * 2, 50)

    @staticmethod
    def _merge_maxp(batch1: list[ScoredUnit], batch2: list[ScoredUnit]) -> list[ScoredUnit]:
        """同 unit_id 取 MaxP，按分降序。batch 1 已排除 batch 2 的重复 id，直接拼。"""
        merged = list(batch1) + list(batch2)
        merged.sort(key=lambda u: u.score, reverse=True)
        return merged


# -- 注册到 RecallerProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@RecallerProducer.register("keyword")
def _build(config):
    # entity_enabled 默认 False（与构建侧 HybridIndexBuilder 同名同义）。开启时
    # 注入 EntityStore 做 L2 实体关联扩展；endpoint 未配时 dep 返 None，recall
    # 侧 _expand_by_entities 自动跳过，不破坏原召回。
    entity_store = None
    if config.get("entity_enabled", False):
        from jiuwen_memory.storage.entity_store import EntityStoreProducer
        entity_store = EntityStoreProducer.dep(config, default="elasticsearch")
    return KeywordRecaller(
        StorageProducer.resolve(config), layer="l2", entity_store=entity_store,
    )


@RecallerProducer.register("keyword_l0")
def _build_l0(config):
    # layers_index_enabled 默认 true；未配置命名端口时该层返回空结果。
    if not config.get("layers_index_enabled", True):
        return KeywordRecaller(StorageProducer.resolve(config), layer="l0")
    recaller = KeywordRecaller(StorageProducer.resolve(config), layer="l0")
    if recaller.fulltext_store is None:
        logger.info("KeywordRecaller(keyword_l0): store 未注入，recall 将返空")
    return recaller


@RecallerProducer.register("keyword_l1")
def _build_l1(config):
    if not config.get("layers_index_enabled", True):
        return KeywordRecaller(StorageProducer.resolve(config), layer="l1")
    recaller = KeywordRecaller(StorageProducer.resolve(config), layer="l1")
    if recaller.fulltext_store is None:
        logger.info("KeywordRecaller(keyword_l1): store 未注入，recall 将返空")
    return recaller
