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

import math
from collections import defaultdict
from statistics import median

from jiuwen_memory.common.type_def.entity import (
    EntityStoreFilters,
    hash_entity_text,
)
from jiuwen_memory.common.type_def.normalizer import EntityNormalizer
from jiuwen_memory.common.type_def.retrieval_filter import is_retrieval_candidate
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

# batch 2（实体关联扩展）候选数硬上限，也是点读真源的次数上限：反查拿到
# 的候选先按 IDF 相关性预筛到这个数，再点读做生产过滤，把 storage.get 的 IO
# 从"反查拉回的全部 id（最坏上千）"压到 ≤ 本上限。
_ENTITY_EXPANSION_TOP_K = 20


class KeywordRecaller(Recaller):
    """关键词/全文召回路（包一个 FulltextStore，支持 content/L0/L1 分层）。"""

    def __init__(
        self,
        storage: Storage,
        *,
        layer: str = "l2",
        entity_store: EntityStore | None = None,
    ) -> None:
        """初始化 KeywordRecaller。

        Args:
            storage: 参数 storage（Storage）。
            layer: 参数 layer（str）。
            entity_store: 参数 entity_store（EntityStore | None）。
        """
        port_name = "default" if layer == "l2" else f"layers_{layer}"
        self._fulltext = (
            storage.fulltext_port(port_name) if storage.has_fulltext_port(port_name) else None
        )
        self._layer = layer  # "l2"(content) | "l0" | "l1"
        self._entity_store = entity_store  # L2 实体关联扩展用；None/非 L2 时不扩展
        # L2 实体扩展需要点读真源做生产过滤（is_retrieval_candidate）；非 L2 不用，
        # 但持有 storage 无成本，装配期统一注入，避免 __init__ 分层条件分支。
        self._storage = storage

    @property
    def layer(self) -> str:
        """当前召回层级（l2/l0/l1），公开只读。"""
        return self._layer

    @property
    def fulltext_store(self) -> FulltextStore | None:
        """注入的全文 store（只读；None 表示该层未注入，recall 返空）。"""
        return self._fulltext

    @staticmethod
    def _entity_list_limit(hash_count: int) -> int:
        """hash→实体记录的 list 上限。按 hash 数给余量，但封顶防过大。"""
        return max(hash_count * 2, 50)

    @staticmethod
    def _merge_maxp(batch1: list[ScoredUnit], batch2: list[ScoredUnit]) -> list[ScoredUnit]:
        """同 unit_id 取 MaxP，按分降序合并 batch 1/2。不负责 top_k 截断——
        截断是 recall 对 ``Recaller.recall`` 契约（≤ top_k）的承诺，在 recall
        里切。batch 1 已排除 batch 2 的重复 id，直接拼即可保证 MaxP。
        """
        merged = list(batch1) + list(batch2)
        merged.sort(key=lambda u: u.score, reverse=True)
        return merged

    def operator_type(self) -> RetrievalOperatorType:
        """返回当前算子类型。

        Returns:
            返回 RetrievalOperatorType。
        """
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def channel(self) -> RecallChannel:
        """执行 `channel` 操作。

        Returns:
            返回 RecallChannel。
        """
        return RecallChannel.KEYWORD

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]:
        """召回与查询匹配的记忆结果。

        Args:
            scope: 参数 scope（Scope）。
            query: 参数 query（ParsedQuery）。
            top_k: 参数 top_k（int）。

        Returns:
            返回 list[ScoredUnit]。
        """
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
        # batch1 已满足 top_k 时短路：实体扩展打分（anchor×decay ≤ batch1 中位数）
        # 注定挤不进 top_k 高分区，反查 + 点读 + 过滤属无用功，直接跳过。
        batch2: list[ScoredUnit] = []
        if len(batch1) < top_k:
            batch2 = self._expand_by_entities(scope, query, records, batch1)

        result = self._merge_maxp(batch1, batch2)[:top_k]
        logger.info(
            "KeywordRecaller: layer=%s scope=%s top_k=%d hits=%d units=%d batch2=%d merged=%d returned=%d%s",
            self._layer, scope, top_k, len(hits), len(batch1), len(batch2),
            len(batch1) + len(batch2), len(result),
            " (short-circuit: batch1>=top_k)" if not batch2 and len(batch1) >= top_k else "",
        )
        return result

    # ------------------------------------------------------------------
    # L2 实体关联扩展
    # ------------------------------------------------------------------

    def _expand_by_entities(
        self,
        scope: Scope,
        query: ParsedQuery,
        records: list,
        batch1: list[ScoredUnit],
    ) -> list[ScoredUnit]:
        """从 batch 1 候选的 entities metadata 反查关联 unit，返回 batch 2 候选。

        不抽 query 实体（零 spaCy）：种子明文来自写入侧 LLM 抽取并落进 L2 文档
        metadata['entities']。打分用中位数锚定 + IDF 抑制高频泛化实体 + cap 软封顶，
        保证关联记忆分不会过低也不会压过 batch 1 原生命中。

        打分量 = Σ_{e∈E_u} idf(df_e)，df_e = len(er.linked_memory_ids) 是该实体
        关联的记忆数（真正的文档频率，user-scope 内）。方向修正：多实体命中 →
        Σ 累加 → 提权（原 decay 用查询命中数 qh 反向衰减是错的）；高频泛化实体
        df 大 → idf 小 → 抑制（检视者例子：A 关联 1000 条，单实体 idf≈0.145）。
        idf(df)=1/log(1+df)。

        生产过滤先于 top_k（S04:40 不变量）：entity_store 只存 unit_id，无
        lifecycle/time/scalar，无法反查时下推过滤。但点读真源可后移——先按 IDF
        相关性预筛到 _ENTITY_EXPANSION_TOP_K（纯内存，无 IO），再点读这 ≤N 个
        真源做 is_retrieval_candidate 过滤。预筛的是相关性、非有效性；有效性过滤
        仍守在召回契约 top_k 之前。反查拉回的全部 id（最坏上千）→ 点读压到 ≤N。

        无效候选（SUPERSEDED/expired/不满足 scalar_filters）若挤进预筛 N，过滤
        后被剔，名单缩水但仍守 top_k 契约——这是容量与假阴性的折中：N=20 让
        高频实体的海量候选先被 IDF 压相关分排到 N 外，再点读，IO 与召回质量平衡。
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

        # 聚合：unit_id → Σ idf(df_e)。df 用 EntityRecord.linked_memory_ids 全长
        # （实体固有属性，不因 batch1 命中浮动）。batch1 已命中 id 不扩展（MaxP 归并）。
        batch1_ids = {u.unit_id for u in batch1}
        raw_contrib: dict[str, float] = defaultdict(float)
        for er in entity_records:
            df = len(er.linked_memory_ids)
            if df <= 0:
                continue
            weight = 1.0 / math.log(1.0 + df)  # idf(df): df=1→1.44, df=10→0.42, df=1000→0.145
            for uid in er.linked_memory_ids:
                if uid in batch1_ids:
                    continue
                raw_contrib[uid] += weight
        if not raw_contrib:
            return []

        # 中位数锚定（rank-based，与 RRF 口径一致——fuser 只看 rank 不看绝对分）。
        scores = [u.score for u in batch1]
        if len(scores) >= 3:
            anchor = median(scores)
        else:
            anchor = (max(scores) if scores else 0.0) * 0.5  # batch1<3 fallback
        if anchor <= 0.0:
            return []
        # cap = max/anchor ≥ 1：保证 score=anchor×min(raw,cap) ≤ anchor×cap = max(batch1)，
        # 严格不压过 batch1 最高分。max==anchor（batch1<3 时 max×0.5=anchor）退化为 cap=2.0。
        cap = (max(scores) / anchor) if scores and anchor > 0 else 1.0

        # 按 raw 相关分预筛到 _ENTITY_EXPANSION_TOP_K（纯内存，无 IO），再点读真源
        # 做生产过滤。点读量从"反查全部 id"压到 ≤ N=20。
        ranked = sorted(raw_contrib.items(), key=lambda kv: kv[1], reverse=True)
        top_n = ranked[:_ENTITY_EXPANSION_TOP_K]
        candidate_ids = [uid for uid, _ in top_n]

        units = {
            u.id: u
            for u in self._storage.get(scope, candidate_ids)
        }
        eligible: list[tuple[str, float]] = []
        for uid, raw in top_n:
            if uid not in units:
                continue
            if not is_retrieval_candidate(
                units[uid],
                as_of=query.as_of,
                time_from=query.time_from,
                time_to=query.time_to,
                filters=query.scalar_filters,
                include_archived=query.include_archived,
            ):
                continue
            eligible.append((uid, raw))
        if not eligible:
            return []

        batch2: list[ScoredUnit] = []
        for uid, raw in eligible:
            boost = min(raw, cap)
            batch2.append(ScoredUnit(
                unit_id=uid,
                score=anchor * boost,
                channel=RecallChannel.KEYWORD,
            ))
        batch2.sort(key=lambda u: u.score, reverse=True)
        return batch2


# -- 注册到 RecallerProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@RecallerProducer.register("keyword")
def _build(config):
    # entity_enabled 默认 False（与构建侧 HybridIndexBuilder 同名同义）。开启时
    # 注入 EntityStore 做 L2 实体关联扩展；endpoint 未配时 dep 返 None，recall
    # 侧 _expand_by_entities 自动跳过，不破坏原召回。
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
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
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    if not config.get("layers_index_enabled", True):
        return KeywordRecaller(StorageProducer.resolve(config), layer="l0")
    recaller = KeywordRecaller(StorageProducer.resolve(config), layer="l0")
    if recaller.fulltext_store is None:
        logger.info("KeywordRecaller(keyword_l0): store 未注入，recall 将返空")
    return recaller


@RecallerProducer.register("keyword_l1")
def _build_l1(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    if not config.get("layers_index_enabled", True):
        return KeywordRecaller(StorageProducer.resolve(config), layer="l1")
    recaller = KeywordRecaller(StorageProducer.resolve(config), layer="l1")
    if recaller.fulltext_store is None:
        logger.info("KeywordRecaller(keyword_l1): store 未注入，recall 将返空")
    return recaller
