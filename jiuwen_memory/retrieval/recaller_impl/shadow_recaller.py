"""文档记忆的 :class:`~retrieval.recaller.Recaller` 实现——影子索引召回。

文档模式（``globals.write_document=true``）下作为 KEYWORD/VECTOR 两路的统一替代：
真源是 ``md`` 文件 + ``shadow`` 影子索引（sqlite3 + sqlite-vec 复合），召回不再走
独立的 fulltext/vector store，而是查影子索引自身的 ``search_fulltext`` /
``search_vector``——影子索引建索引时已把 ``unit_json`` / FTS5 倒排 / vec0 向量
三表收进同一 db，召回与真源同库同事务。

一个 ShadowRecaller 同时承载关键词与向量两种查询能力（影子索引本就是复合算子），
``ParsedQuery`` 里有 ``vector`` 时走 ANN、否则走 FTS5——与 KEYWORD/VECTOR 两路
recaller 并列但合并为单通道，通道值取 ``RecallChannel.DOCUMENT``（语义即文档记忆召回）。

**本文件当前为接口骨架（F08 §5.6）：** recall 主流程已接通 ``shadow.search_fulltext``
（核心关键词召回可跑），向量路、实体扩展、生产过滤、ScoredID→ScoredUnit 的完整聚合
按依赖进度逐步补齐——详见各方法内 ``# TODO(F08 §5.6)`` 标注。

与 KeywordRecaller/VectorRecaller 的分工：那两者取 ``storage.fulltext_port`` /
``storage.vector_port``（KV 时代端口，文档模式为 None → recall 返空）；本算子取
``storage.shadow_index_port``（文档模式必有端口），是文档模式下唯一真正命中的召回路。
"""

from __future__ import annotations

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.recaller import Recaller, RecallerProducer
from jiuwen_memory.retrieval.types import ParsedQuery, RecallChannel, ScoredUnit
from jiuwen_memory.storage.shadow import DocumentShadowIndex
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import ScoredID, TextQuery, VectorQuery

logger = get_logger(__name__)


class ShadowRecaller(Recaller):
    """影子索引召回——文档模式 KEYWORD/VECTOR 的统一替代。

    构造即解析：``storage`` 无 shadow 端口时直接抛
    :class:`~common.errors.UnsupportedStorageCapabilityError`，不拖到首次 recall 才暴露
    ——与 :class:`DocumentIndexBuilder` 构造期校验同范式。文档模式下 shadow 端口必就绪，
    装配到这里为 None 即说明装配分流未生效，应 fail-closed 而非静默返空。
    """

    def __init__(self, storage: Storage) -> None:
        if not storage.has_shadow_port():
            raise UnsupportedStorageCapabilityError(
                "ShadowRecaller 要求 shadow 端口就绪（文档模式 globals.write_document=true），"
                "但注入的 Storage 无 shadow 端口"
            )
        self._shadow: DocumentShadowIndex = storage.shadow_index_port()
        # 持有 storage 供后续生产过滤点读真源（is_retrieval_candidate）使用——与
        # KeywordRecaller 同范式：L2 扩展需点读真源做生产过滤，装配期统一注入。
        self._storage = storage

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        """文档记忆召回通道（关键词+向量合一）。

        与 KV 时代 keyword/vector/graph 三路并列的独立第四路：文档模式下 retriever
        装配本算子替代前三路，``channel()=DOCUMENT``。parser 产出的
        ``parsed.channels`` 默认含 ``[KEYWORD, VECTOR, GRAPH]`` 不含 DOCUMENT，
        故 retriever 文档模式需把 DOCUMENT 注入 enabled channels（见
        ``pipeline_retriever.retrieve`` 的 channels 补全），否则本算子被
        ``storage._recall`` 的 ``r.channel() in channels`` 过滤掉 → 召回落空
        （F08 §5.6 S14 根因之二）。
        """
        return RecallChannel.DOCUMENT

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]:
        """影子索引召回：fulltext 主路 + vector 补充路，两路并行合并。

        **fulltext 必跑**（FTS5 BM25，影子索引核心召回能力）。**vector 补充**：
        仅当影子索引向量模式可用（``shadow.vec_enabled``，完整模式建了 memory_vec
        表）且 ``query.vector`` 非空时跑 ANN，把向量命中的候选补进来——降级模式
        （无 embedder/sqlite_vec）或 query 无向量时，vector 路返空，单走 fulltext。

        两路结果按 ``unit_id`` 做 RRF（Reciprocal Rank Fusion）名次合并——存储端
        两路返回时已按相关度排好序（fulltext 按 bm25 升序、vector 按 distance
        升序，最相关在前），名次即列表下标，召回层不解释任何原始分数量纲，
        降序取 top_k。``ScoredID.metadata`` 暂不透传进 ``evidence``——物化侧从
        ``shadow.get_units`` 重取完整 ``MemoryUnit``，metadata 在召回阶段非必需。
        """
        # 主路：fulltext 必跑。
        scored_ids = self._recall_fulltext(scope, query, top_k)
        # 补充路：向量模式可用且 query.vector 非空时跑 ANN。
        # 降级模式（vec_enabled=False）即使 query.vector 有值（QueryParser 内嵌
        # HashingEmbedder 会给查询生成向量）也不跑——search_vector 返空。
        vec_ids: list = []
        if query.vector and getattr(self._shadow, "vec_enabled", False):
            vec_ids = self._recall_vector(scope, query, top_k)

        # 两路按名次 RRF 合并（方向/量纲无关，见 _merge_rrf）。
        paths = [scored_ids, vec_ids]
        result = self._merge_rrf(paths, top_k)
        logger.info(
            "ShadowRecaller: scope=%s top_k=%d fulltext=%d vector=%d returned=%d",
            scope, top_k, len(scored_ids), len(vec_ids), len(result),
        )
        return result

    @staticmethod
    def _merge_rrf(paths: list[list[ScoredID]], top_k: int) -> list[ScoredUnit]:
        """多路候选拆名次 RRF 合并（k=60，与 RRFFuser 默认一致）。

        **为何不按分数 max 归并**：存储端两路的分数口径相反且量纲不可比——
        ``search_fulltext`` 返回原始 FTS5 bm25（负值，越小越相关），
        ``search_vector`` 返回负距离（越大越相关）。若在此统一翻正/取 max，
        vector 路会被二次取负变回距离（越大越不相关）导致排序整体颠倒；
        即使按路分别换算，-bm25（无界对数）与 distance（欧氏距离）量纲
        不可比，max 归并会让 fulltext 恒压制 vector。RRF 只消费名次
        （两路返回时已按相关度排好序，名次即列表下标），方向与量纲问题
        一次性消除；同一 unit 两路都命中时贡献累加（多路一致更可信）。
        """
        k = 60
        contrib: dict[str, float] = {}
        for path in paths:
            for rank, sid in enumerate(path):
                contrib[sid.id] = contrib.get(sid.id, 0.0) + 1.0 / (k + rank + 1)
        ranked = sorted(contrib.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [
            ScoredUnit(unit_id=uid, score=score, channel=RecallChannel.DOCUMENT)
            for uid, score in ranked
        ]

    # ------------------------------------------------------------------
    # 召回子路径（关键词 / 向量）——接口已定义，实现按依赖进度补齐
    # ------------------------------------------------------------------

    def _recall_fulltext(self, scope: Scope, query: ParsedQuery, top_k: int):
        """FTS5 关键词召回：用 ``query.rewritten or query.raw`` 构造 ``TextQuery``。

        ``query.scalar_filters`` 落 ``TextQuery.filters`` 做元数据硬过滤（与
        KeywordRecaller 同口径）；scope 不混进 filters，走 ``search_fulltext`` 入参
        做原生隔离。返回 ``ScoredID`` 列表（id+score+metadata）。
        """
        tq = TextQuery(
            text=query.rewritten or query.raw,
            top_k=top_k,
            filters=query.scalar_filters,
        )
        return self._shadow.search_fulltext(scope, tq)

    def _recall_vector(self, scope: Scope, query: ParsedQuery, top_k: int):
        """sqlite-vec ANN 召回：用 ``query.vector`` 构造 ``VectorQuery``。

        代码已实现（VectorQuery 构造 + shadow.search_vector 调用齐备），联调验证待
        F08 §5.6 全链路跑通时补——查询向量维度须与影子索引建表时 ``embedder_dim``
        一致（装配期固化），post-filter 过采样兜底属算子内部已处理的机制。
        ``return_metadata`` 取默认 False：物化侧走 ``shadow.get_units`` 重取完整
        ``MemoryUnit``，召回阶段不需 metadata。
        """
        vq = VectorQuery(
            vector=list(query.vector),
            top_k=top_k,
            filters=query.scalar_filters,
        )
        return self._shadow.search_vector(scope, vq)

    # TODO(F08 §5.6): 生产过滤（is_retrieval_candidate）——召回出 ScoredID 后，
    #   点读真源做生命周期/时间/标签过滤，与 KeywordRecaller._expand_by_entities
    #   的 eligible 过滤同口径。当前 materialize 侧（CompositeStorage.get 文档分流）
    #   尚未落地，过滤点待 storage.get 分流（步骤2）就绪后在此补。


# -- 注册到 RecallerProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@RecallerProducer.register("shadow")
def _build(config):
    return ShadowRecaller(StorageProducer.resolve(config))
