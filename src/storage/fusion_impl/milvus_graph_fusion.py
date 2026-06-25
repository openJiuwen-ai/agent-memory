"""MilvusGraphFusionStore — Milvus(向量) + nano-graphrag(图) 融合的 FusionStore。

后端为 Milvus(向量) + nano-graphrag(图) 的融合：构建时同时建向量与图索引，检索时
向量召回后再经图扩展邻居。第三方依赖惰性加载，未安装也可导入本包并完成注册。

实现 :class:`~storage.fusion.FusionStore`，**构建时同时建图与向量索引**、**检索时
先向量召回再图扩展邻居**：

- **写入（insert/update）**：每条 :class:`FusionRecord` 的向量/标量/文本/原始值落
  Milvus（向量索引 + 标量过滤 + 按 id 正排），同时把该记录作为节点写入
  nano-graphrag 图；记录在保留标量字段 ``link_field``（默认 ``"links"``，值为邻居
  id 列表）声明的邻接关系建成图的边。图侧用 nano-graphrag 的 **upsert**（幂等、可
  前向引用），不走严格 CRUD —— 它是融合内部的索引而非对外的图 store。
- **检索（search）**：先用 ``query.vector`` 在 Milvus 做 ANN 召回得到种子，再沿图把
  每个种子的邻居（最多 ``neighbor_depth`` 跳）一并召回，邻居得分按
  ``neighbor_decay ** hop`` 从种子分衰减；种子（含向量分）与邻居合并去重、降序返回。
  因此结果是「向量 top-k 的种子 + 其图邻居」，条数可超过 ``top_k``。

组合既有的 :class:`~storage.vector_impl.milvus_vector.MilvusVectorStore`（向量 + 正排
存储）与 nano-graphrag ``NetworkXStorage``（邻接），各自的 scope 原生隔离自然继承。

说明：本融合形态聚焦「向量 → 图」，未使用 ``FusionQuery.text`` / ``vector_weight``
（文本仅随记录存储供 ``get`` 回读，不做 BM25）。
"""

from __future__ import annotations

import base64
from typing import Any

from common.errors import HealthCheckError, ValidationError
from common.factory.factory import Factory
from common.type_def import Scope
from storage.fusion import FusionProducer

from .._support import scope_segments, wrap_backend
from ..base import StoreType
from ..fusion import FusionStore
from ..graph_impl.nano_graphrag_graph import _networkx_storage_cls, networkx_graph
from ..types import FusionQuery, FusionRecord, ScoredID, VectorQuery, VectorRecord
from ..vector_impl.milvus_vector import MilvusVectorStore

_TEXT_KEY = "_fusion_text"
_VALUE_KEY = "_fusion_value_b64"


class MilvusGraphFusionStore(FusionStore):
    def __init__(
        self,
        *,
        working_dir: str,
        dim: int,
        milvus: dict[str, Any] | None = None,
        collection: str = "agent_memory_fusion",
        metric_type: str = "COSINE",
        namespace_prefix: str = "agent_memory_fusion_graph",
        link_field: str = "links",
        neighbor_depth: int = 1,
        neighbor_decay: float = 0.5,
        neighbor_relation: str = "linked",
        **options: Any,
    ) -> None:
        import asyncio  # 仅桥接异步图存储用

        mcfg = dict(milvus or {})
        mcfg.setdefault("collection", collection)
        mcfg.setdefault("metric_type", metric_type)
        # 向量 + 正排存储复用 Milvus 适配器（它负责 scope 隔离/冲突/缺失语义）。
        self._vec = MilvusVectorStore(dim=dim, **mcfg)

        self._workdir = working_dir
        self._prefix = namespace_prefix
        self._link_field = link_field
        self._depth = neighbor_depth
        self._decay = neighbor_decay
        self._relation = neighbor_relation
        self._options = options
        self._graphs: dict[str, Any] = {}  # namespace -> NetworkXStorage
        self._loop: asyncio.AbstractEventLoop | None = None
        from pathlib import Path

        Path(self._workdir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 图基础设施
    def _run(self, coro: Any) -> Any:
        import asyncio

        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def _graph(self, scope: Scope) -> Any:
        ns = f"{self._prefix}-" + "_".join(scope_segments(scope))
        storage = self._graphs.get(ns)
        if storage is None:
            cls = _networkx_storage_cls()
            with wrap_backend("fusion graph open"):
                storage = cls(namespace=ns, global_config={"working_dir": self._workdir})
            self._graphs[ns] = storage
        return storage

    def _persist(self, storage: Any) -> None:
        with wrap_backend("fusion graph persist"):
            self._run(storage.index_done_callback())

    def _links(self, record: FusionRecord) -> list[str]:
        raw = record.scalars.get(self._link_field) or []
        if isinstance(raw, (list, tuple, set)):
            return [str(x) for x in raw]
        return []

    def _index_graph(self, scope: Scope, records: list[FusionRecord]) -> None:
        storage = self._graph(scope)

        async def apply() -> None:
            for rec in records:
                await storage.upsert_node(rec.id, {"id": rec.id})
                for target in self._links(rec):
                    await storage.upsert_edge(rec.id, target, {"relation": self._relation})

        with wrap_backend("fusion graph index"):
            self._run(apply())
        self._persist(storage)

    # ------------------------------------------------------------ 序列化
    def _to_vector_record(self, record: FusionRecord) -> VectorRecord:
        if record.vector is None:
            raise ValidationError(f"fusion record {record.id!r} requires a vector")
        metadata = dict(record.scalars)
        metadata[_TEXT_KEY] = record.text
        metadata[_VALUE_KEY] = (
            base64.b64encode(record.value).decode("ascii") if record.value is not None else None
        )
        return VectorRecord(id=record.id, vector=record.vector, metadata=metadata)

    @staticmethod
    def _from_vector_record(vr: VectorRecord) -> FusionRecord:
        metadata = dict(vr.metadata)
        text = metadata.pop(_TEXT_KEY, None)
        vb64 = metadata.pop(_VALUE_KEY, None)
        value = base64.b64decode(vb64) if vb64 else None
        return FusionRecord(
            id=vr.id,
            vector=vr.vector or None,
            text=text,
            scalars=metadata,
            value=value,
        )

    # ------------------------------------------------------------ 契约
    def store_type(self) -> StoreType:
        return StoreType.FUSION

    def health(self) -> None:
        self._vec.health()
        try:
            _networkx_storage_cls()
        except Exception as exc:
            raise HealthCheckError(f"fusion graph unavailable: {exc}") from exc

    def insert(self, scope: Scope, records: list[FusionRecord]) -> None:
        if not records:
            return
        vrecs = [self._to_vector_record(r) for r in records]  # 校验 vector
        self._vec.insert(scope, vrecs)  # id 冲突 -> ConflictError
        self._index_graph(scope, records)

    def update(self, scope: Scope, records: list[FusionRecord]) -> None:
        if not records:
            return
        vrecs = [self._to_vector_record(r) for r in records]
        self._vec.update(scope, vrecs)  # 缺失 -> NotFoundError
        # upsert 节点属性 + 追加新链边（移除旧链请用 delete+insert）
        self._index_graph(scope, records)

    def delete(self, scope: Scope, ids: list[str]) -> None:
        if not ids:
            return
        self._vec.delete(scope, ids)
        storage = self._graph(scope)
        graph = networkx_graph(storage)
        with wrap_backend("fusion graph delete"):
            for i in ids:
                if graph.has_node(i):
                    graph.remove_node(i)  # 连带其边
        self._persist(storage)

    def get(self, scope: Scope, ids: list[str]) -> list[FusionRecord]:
        if not ids:
            return []
        return [self._from_vector_record(vr) for vr in self._vec.get(scope, ids)]

    def search(self, scope: Scope, query: FusionQuery) -> list[ScoredID]:
        if query.vector is None:
            raise ValidationError("fusion search requires a query vector (vector → graph)")
        # ① Milvus 向量召回种子
        seeds = self._vec.search(
            scope,
            VectorQuery(vector=query.vector, top_k=query.top_k, filters=query.scalar_filters),
        )
        # ② 图扩展邻居（按跳数衰减），与种子合并去重、取最大分
        merged: dict[str, float] = self._expand_neighbors(scope, seeds)
        for s in seeds:  # 种子的向量分覆盖其作为他人邻居的衰减分
            merged[s.id] = s.score
        out = [ScoredID(id=i, score=sc) for i, sc in merged.items()]
        out.sort(key=lambda x: x.score, reverse=True)
        return out

    def _expand_neighbors(self, scope: Scope, seeds: list[ScoredID]) -> dict[str, float]:
        storage = self._graph(scope)
        graph = networkx_graph(storage)
        out: dict[str, float] = {}
        with wrap_backend("fusion graph expand"):
            for seed in seeds:
                if not graph.has_node(seed.id):
                    continue
                visited = {seed.id}
                frontier = [seed.id]
                for hop in range(1, self._depth + 1):
                    nxt: list[str] = []
                    decayed = seed.score * (self._decay**hop)
                    for node in frontier:
                        for neighbor in graph.neighbors(node):
                            if neighbor in visited:
                                continue
                            visited.add(neighbor)
                            nxt.append(neighbor)
                            if out.get(neighbor, float("-inf")) < decayed:
                                out[neighbor] = decayed
                    frontier = nxt
                    if not frontier:
                        break
        return out


# -- 注册到 FusionProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@FusionProducer.register("milvus_graph")
def _build(config):
    # 三方库 + 本地图：uri/working_dir 必填；dim 取 params.dim，回退内核共享的 embedder_dim。
    # 其余有默认值的构造参数均可经 params 覆盖。
    uri = Factory.require_param(config, "uri", backend="milvus_graph fusion")
    return MilvusGraphFusionStore(
        working_dir=Factory.require_param(config, "working_dir", backend="milvus_graph fusion"),
        dim=Factory.cfg_get(config, "dim", Factory.cfg_get(config, "embedder_dim", 0)),
        milvus={"uri": uri},
        collection=Factory.cfg_get(config, "collection", "agent_memory_fusion"),
        metric_type=Factory.cfg_get(config, "metric_type", "COSINE"),
        namespace_prefix=Factory.cfg_get(config, "namespace_prefix", "agent_memory_fusion_graph"),
        link_field=Factory.cfg_get(config, "link_field", "links"),
        neighbor_depth=Factory.cfg_get(config, "neighbor_depth", 1),
        neighbor_decay=Factory.cfg_get(config, "neighbor_decay", 0.5),
        neighbor_relation=Factory.cfg_get(config, "neighbor_relation", "linked"),
    )
