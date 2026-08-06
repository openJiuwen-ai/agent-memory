"""NanoGraphRAGGraphStore — 基于 nano-graphrag ``NetworkXStorage`` 的 GraphStore 实现。

后端基于 nano-graphrag 的 ``NetworkXStorage``；第三方依赖惰性加载，未安装也可
导入本包并完成工厂注册，仅在创建并访问图存储时才需要依赖就绪。

nano-graphrag 的默认图存储 ``NetworkXStorage`` 是对 networkx 的薄封装：节点/边
upsert、按 id 点查、邻接遍历，并以 GraphML 持久化。本适配器复用它承载属性图，
对接 :class:`~storage.graph.GraphStore` 的统一 CRUD + 邻域遍历。

要点：

- **惰性加载且避开重依赖**：``import nano_graphrag`` 会急切拉起整条 GraphRAG 流水线
  （openai/tiktoken/graspologic/dspy/hnswlib/neo4j，在新版 Python 上多无法构建）。
  而图存储本身只需 networkx + numpy + tiktoken。故用一段 shim 直接加载
  ``nano_graphrag._storage.gdb_networkx``，跳过包 ``__init__`` 的重导入。加载发生在
  首次创建/访问存储时（``import storage`` 不触发）。
- **scope 原生隔离**：每个 scope 对应一个独立 namespace（→ 独立 GraphML 文件），
  写入/遍历天然不跨 scope。
- **同步↔异步桥接**：``NetworkXStorage`` 方法为 async（实为内存操作），用一个常驻
  事件循环驱动；删除走底层 ``_graph``（``NetworkXStorage`` 未提供删除）。
- **边标识**：``nx.Graph`` 按 ``(source, target)`` 唯一存边（非多重图），边的逻辑
  ``id`` 存为属性；按 id 的删除/更新经反查 ``id -> (u, v)`` 定位。同一对端点至多一条
  边——对已存在端点对再插入新边按冲突处理。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from common.errors import (
    BackendError,
    ConflictError,
    HealthCheckError,
    NotFoundError,
)
from common.factory.factory import Factory
from common.type_def import Scope
from storage.graph import GraphProducer

from .._support import scope_segments, wrap_backend
from ..base import StoreType
from ..graph import GraphStore
from ..types import Edge, GraphQuery, Node

_NX_STORAGE_CLS: Any = None


def _networkx_storage_cls() -> Any:
    """惰性加载 nano-graphrag 的 ``NetworkXStorage`` 类（绕过包重 ``__init__``）。"""
    global _NX_STORAGE_CLS
    if _NX_STORAGE_CLS is not None:
        return _NX_STORAGE_CLS
    from importlib.machinery import PathFinder

    spec = PathFinder.find_spec("nano_graphrag")
    if spec is None or spec.origin is None:
        raise BackendError("nano-graphrag not installed (pip install nano-graphrag)")
    pkg_dir = os.path.dirname(spec.origin)
    import importlib
    import types

    # 用轻量 stub 占位包与子包，使导入存储子模块时不执行 nano_graphrag/__init__。
    for name, sub in (("nano_graphrag", ""), ("nano_graphrag._storage", "_storage")):
        existing = sys.modules.get(name)
        if existing is None or not hasattr(existing, "__path__"):
            stub = types.ModuleType(name)
            stub.__path__ = [pkg_dir if not sub else os.path.join(pkg_dir, sub)]
            sys.modules[name] = stub
    try:
        module = importlib.import_module("nano_graphrag._storage.gdb_networkx")
    except Exception as exc:
        raise BackendError(f"failed to load nano-graphrag NetworkXStorage: {exc}") from exc
    _NX_STORAGE_CLS = module.NetworkXStorage
    return _NX_STORAGE_CLS


def networkx_graph(storage: Any) -> Any:
    """集中读取 nano-graphrag 的底层 NetworkX graph 对象。"""
    return getattr(storage, "_graph")


class NanoGraphRAGGraphStore(GraphStore):
    def __init__(
        self,
        *,
        working_dir: str,
        namespace_prefix: str = "agent_memory_graph",
        create_root: bool = True,
        config_source=None,
        config_namespace: str = "graph_store",
        **options: Any,
    ) -> None:
        self._fallback_working_dir = str(Path(working_dir).resolve())
        self._config_source = config_source
        self._config_namespace = config_namespace
        self._prefix = namespace_prefix
        self._create_root = create_root
        self._options = options
        self._storages: dict[str, Any] = {}  # namespace -> NetworkXStorage
        self._active_working_dir: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        if create_root:
            Path(self._fallback_working_dir).mkdir(parents=True, exist_ok=True)

    def _resolved_working_dir(self) -> str:
        from config.binding import resolve_connection_url

        live = resolve_connection_url(
            self._config_source,
            namespace=self._config_namespace,
            field="working_dir",
            fallback=self._fallback_working_dir,
        )
        path = str(Path(live or self._fallback_working_dir).resolve())
        if self._create_root:
            Path(path).mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------ 基础设施
    def _run(self, coro: Any) -> Any:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def _namespace(self, scope: Scope) -> str:
        return f"{self._prefix}-" + "_".join(scope_segments(scope))

    def _store(self, scope: Scope) -> Any:
        working_dir = self._resolved_working_dir()
        if self._active_working_dir != working_dir:
            # working_dir 切换：丢弃旧 scope 缓存（旧图文件仍在旧目录，不迁移）
            self._storages.clear()
            self._active_working_dir = working_dir
        ns = self._namespace(scope)
        storage = self._storages.get(ns)
        if storage is None:
            cls = _networkx_storage_cls()
            with wrap_backend("nano-graphrag open"):
                storage = cls(namespace=ns, global_config={"working_dir": working_dir})
            self._storages[ns] = storage
        return storage

    def _persist(self, storage: Any) -> None:
        with wrap_backend("nano-graphrag persist"):
            self._run(storage.index_done_callback())

    # ------------------------------------------------------------ 序列化
    @staticmethod
    def _node_data(node: Node) -> dict[str, str]:
        return {"label": node.label, "properties": json.dumps(node.properties)}

    @staticmethod
    def _edge_data(edge: Edge) -> dict[str, str]:
        return {"id": edge.id, "relation": edge.relation, "properties": json.dumps(edge.properties)}

    @staticmethod
    def _to_node(node_id: str, data: dict[str, Any]) -> Node:
        return Node(
            id=node_id,
            label=data.get("label", ""),
            properties=json.loads(data.get("properties") or "{}"),
        )

    @staticmethod
    def _find_edge(graph: Any, edge_id: str) -> tuple[str, str] | None:
        for u, v, data in graph.edges(data=True):
            if data.get("id") == edge_id:
                return (u, v)
        return None

    # ------------------------------------------------------------ 契约
    def store_type(self) -> StoreType:
        return StoreType.GRAPH

    def health(self) -> None:
        try:
            _networkx_storage_cls()
        except BackendError as exc:
            raise HealthCheckError(str(exc)) from exc
        if not Path(self._resolved_working_dir()).is_dir():
            raise HealthCheckError(
                f"graph working_dir not a directory: {self._resolved_working_dir()}"
            )

    def seed_ids(self, scope: Scope, tokens: set[str]) -> list[str]:
        if not tokens:
            return []
        storage = self._store(scope)
        graph = networkx_graph(storage)
        out: list[str] = []
        with wrap_backend("nano-graphrag seed_ids"):
            for nid, data in graph.nodes(data=True):
                content = str(self._to_node(nid, data).properties.get("content", ""))
                if any(tok and tok in content for tok in tokens):
                    out.append(nid)
        return out

    def insert(
        self,
        scope: Scope,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        nodes = nodes or []
        edges = edges or []
        storage = self._store(scope)
        graph = networkx_graph(storage)
        # 冲突预检（先验后写）
        for node in nodes:
            if graph.has_node(node.id):
                raise ConflictError(entity="node", key=node.id)
        for edge in edges:
            if self._find_edge(graph, edge.id) is not None:
                raise ConflictError(entity="edge", key=edge.id)
            if graph.has_edge(edge.source, edge.target):
                raise ConflictError(
                    entity="edge",
                    key=edge.id,
                    message=f"edge already exists for pair ({edge.source}, {edge.target})",
                )

        async def apply() -> None:
            for node in nodes:
                await storage.upsert_node(node.id, self._node_data(node))
            for edge in edges:
                await storage.upsert_edge(edge.source, edge.target, self._edge_data(edge))

        with wrap_backend("nano-graphrag insert"):
            self._run(apply())
        self._persist(storage)

    def update(
        self,
        scope: Scope,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        nodes = nodes or []
        edges = edges or []
        storage = self._store(scope)
        graph = networkx_graph(storage)
        for node in nodes:
            if not graph.has_node(node.id):
                raise NotFoundError(entity="node", key=node.id)
        edge_locs: dict[str, tuple[str, str]] = {}
        for edge in edges:
            loc = self._find_edge(graph, edge.id)
            if loc is None:
                raise NotFoundError(entity="edge", key=edge.id)
            edge_locs[edge.id] = loc

        async def apply() -> None:
            for node in nodes:
                await storage.upsert_node(node.id, self._node_data(node))
            for edge in edges:
                u, v = edge_locs[edge.id]  # 保持原端点，仅更新关系/属性
                await storage.upsert_edge(u, v, self._edge_data(edge))

        with wrap_backend("nano-graphrag update"):
            self._run(apply())
        self._persist(storage)

    def delete(
        self,
        scope: Scope,
        node_ids: list[str] | None = None,
        edge_ids: list[str] | None = None,
    ) -> None:
        storage = self._store(scope)
        graph = networkx_graph(storage)
        with wrap_backend("nano-graphrag delete"):
            for nid in node_ids or []:
                if graph.has_node(nid):
                    graph.remove_node(nid)  # 连带其关联边
            for eid in edge_ids or []:
                loc = self._find_edge(graph, eid)
                if loc is not None:
                    graph.remove_edge(*loc)
        self._persist(storage)

    def get(self, scope: Scope, node_ids: list[str]) -> list[Node]:
        if not node_ids:
            return []
        storage = self._store(scope)
        out: list[Node] = []

        async def fetch() -> None:
            for nid in node_ids:
                data = await storage.get_node(nid)
                if data is not None:
                    out.append(self._to_node(nid, data))

        with wrap_backend("nano-graphrag get"):
            self._run(fetch())
        return out

    def search(self, scope: Scope, query: GraphQuery) -> list[Node]:
        storage = self._store(scope)
        graph = networkx_graph(storage)
        if not graph.has_node(query.start_id):
            return []
        with wrap_backend("nano-graphrag search"):
            visited = {query.start_id}
            frontier = [query.start_id]
            found: list[str] = []
            for _ in range(max(query.depth, 0)):
                nxt: list[str] = []
                for node in frontier:
                    for neighbor in graph.neighbors(node):
                        edge_data = graph.get_edge_data(node, neighbor) or {}
                        if (
                            query.relation is not None
                            and edge_data.get("relation") != query.relation
                        ):
                            continue
                        if neighbor not in visited:
                            visited.add(neighbor)
                            nxt.append(neighbor)
                            found.append(neighbor)
                            if len(found) >= query.limit:
                                break
                    if len(found) >= query.limit:
                        break
                frontier = nxt
                if not frontier or len(found) >= query.limit:
                    break
            found = found[: query.limit]
            return [self._to_node(nid, graph.nodes[nid]) for nid in found]


# -- 注册到 GraphProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@GraphProducer.register("nano_graphrag")
def _build(config):
    # working_dir 在构造器中无默认值 → 必填；namespace_prefix/create_root 有默认值，可覆盖。
    from config.config_source import ConfigSourceProducer

    return NanoGraphRAGGraphStore(
        working_dir=Factory.require_param(config, "working_dir", backend="nano_graphrag graph"),
        namespace_prefix=Factory.cfg_get(config, "namespace_prefix", "agent_memory_graph"),
        create_root=Factory.cfg_get(config, "create_root", True),
        config_source=ConfigSourceProducer.get_cached("default"),
    )
