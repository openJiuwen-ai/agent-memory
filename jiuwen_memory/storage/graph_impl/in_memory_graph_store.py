# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~storage.graph.GraphStore` 的纯内存属性图。

按 scope 隔离地存节点/边，``search`` 从 ``start_id`` BFS 扩展邻域（按 depth 跳数、
可选 relation 过滤，边按无向遍历）。无外部图库依赖。

``seed_ids``（GraphStore 契约方法）：按关键词在节点 ``content`` 属性里找种子
节点——供图召回路在「无 query 起点」时定位起点（契约的 ``search`` 要求
``start_id``，而 query 侧并不携带）。本实现以「属性子串命中」匹配。
"""

from __future__ import annotations

from collections import defaultdict

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.capability import StoreCapability
from jiuwen_memory.storage.graph import GraphProducer, GraphStore
from jiuwen_memory.storage.shard_search import graph_node_allowed, graph_shard_filter
from jiuwen_memory.storage.types import Edge, GraphQuery, Node

_ScopeKey = tuple[str, str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


class InMemoryGraphStore(GraphStore):
    """纯内存属性图：节点/边按 scope 隔离，BFS 邻域遍历。"""

    def __init__(self) -> None:
        self._nodes: dict[_ScopeKey, dict[str, Node]] = defaultdict(dict)
        self._edges: dict[_ScopeKey, dict[str, Edge]] = defaultdict(dict)
        self._tombs: dict[_ScopeKey, dict[str, int]] = defaultdict(dict)

    @property
    def supports_incident_edges(self) -> bool:
        return True

    def capabilities(self) -> frozenset[StoreCapability]:
        # 共享图在遍历前按节点 storage_shard 过滤，可作 shared 模式内层。
        return frozenset({StoreCapability.SHARD_PREFILTER})

    @property
    def supports_conditional_projection(self) -> bool:
        return True

    def project_edge(self, scope: Scope, edge: Edge, *, revision: int, nodes=None) -> None:
        sk = _skey(scope)
        for node in nodes or []:
            self._nodes[sk][node.id] = node  # merge: create or reuse, never delete
        props = {**(edge.properties or {}), "revision": revision}
        projected = Edge(edge.id, edge.source, edge.target, edge.relation, props)
        current = self._edges[sk].get(edge.id)
        tomb = self._tombs[sk].get(edge.id, 0)
        if current is None:
            if tomb >= revision:
                raise ConflictError("edge", edge.id)
            self._edges[sk][edge.id] = projected
            return
        existing = int((current.properties or {}).get("revision") or 0)
        if existing == revision:
            if current == projected:
                return
            raise ConflictError("edge", edge.id)
        if existing > revision:
            raise ConflictError("edge", edge.id)
        self._edges[sk][edge.id] = projected

    def unproject_edge(self, scope: Scope, edge_id: str, *, revision: int) -> None:
        sk = _skey(scope)
        current = self._edges[sk].get(edge_id)
        self._tombs[sk][edge_id] = max(self._tombs[sk].get(edge_id, 0), revision)
        if current is None:
            return
        if int((current.properties or {}).get("revision") or 0) > revision:
            return
        self._edges[sk].pop(edge_id, None)

    def incident_edges(self, scope: Scope, node_id: str, *, limit: int = 100) -> list[Edge]:
        from copy import deepcopy

        return deepcopy(sorted(
            (e for e in self._edges[_skey(scope)].values()
             if e.source == node_id or e.target == node_id),
            key=lambda e: e.id,
        )[:limit])

    def store_type(self) -> StoreType:
        return StoreType.GRAPH

    def health(self) -> None:
        return None

    def close(self) -> None:
        return None

    def insert(
        self,
        scope: Scope,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        sk = _skey(scope)
        for n in nodes or []:
            if n.id in self._nodes[sk]:
                raise ConflictError("node", n.id)
            self._nodes[sk][n.id] = n
        for e in edges or []:
            if e.id in self._edges[sk]:
                raise ConflictError("edge", e.id)
            self._edges[sk][e.id] = e

    def update(
        self,
        scope: Scope,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        sk = _skey(scope)
        for n in nodes or []:
            if n.id not in self._nodes[sk]:
                raise NotFoundError("node", n.id)
            self._nodes[sk][n.id] = n
        for e in edges or []:
            if e.id not in self._edges[sk]:
                raise NotFoundError("edge", e.id)
            self._edges[sk][e.id] = e

    def delete(
        self,
        scope: Scope,
        node_ids: list[str] | None = None,
        edge_ids: list[str] | None = None,
    ) -> None:
        sk = _skey(scope)
        for nid in node_ids or []:
            self._nodes[sk].pop(nid, None)
            for eid in [
                e.id for e in self._edges[sk].values()
                if e.source == nid or e.target == nid
            ]:
                self._edges[sk].pop(eid, None)  # 连带删关联边
        for eid in edge_ids or []:
            self._edges[sk].pop(eid, None)

    def get(self, scope: Scope, node_ids: list[str]) -> list[Node]:
        bucket = self._nodes[_skey(scope)]
        return [bucket[i] for i in node_ids if i in bucket]

    def search(self, scope: Scope, query: GraphQuery) -> list[Node]:
        selected = graph_shard_filter(query.extensions)
        sk = _skey(scope)
        start = self._nodes[sk].get(query.start_id)
        if start is not None and not graph_node_allowed(start.properties, selected):
            return []
        adj: dict[str, list[str]] = defaultdict(list)
        for e in self._edges[sk].values():
            if query.relation and e.relation != query.relation:
                continue
            adj[e.source].append(e.target)  # 无向遍历
            adj[e.target].append(e.source)
        visited: set[str] = {query.start_id}
        frontier = [query.start_id]
        reached: list[str] = []
        for _ in range(max(query.depth, 0)):
            nxt: list[str] = []
            for nid in frontier:
                for nb in adj.get(nid, []):
                    if nb not in visited:
                        visited.add(nb)
                        nxt.append(nb)
                        reached.append(nb)
            frontier = nxt
        nodes: list[Node] = []
        for node_id in reached:
            node = self._nodes[sk].get(node_id)
            if node is not None and graph_node_allowed(node.properties, selected):
                nodes.append(node)
        return nodes[: query.limit]

    # -- GraphStore 契约：按关键词找种子节点（属性子串命中） ------------------ #
    def seed_ids(
        self,
        scope: Scope,
        tokens: set[str],
        *,
        extensions: dict | None = None,
    ) -> list[str]:
        if not tokens:
            return []
        selected = graph_shard_filter(extensions)
        out: list[str] = []
        for nid, node in self._nodes[_skey(scope)].items():
            if not graph_node_allowed(node.properties, selected):
                continue
            content = str(node.properties.get("content", ""))
            if any(tok and tok in content for tok in tokens):
                out.append(nid)
        return out


# -- 注册到 GraphProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@GraphProducer.register(
    "memory", config_inspectors={"at_rest_locations": lambda config: {}}
)
def _build(config):
    return InMemoryGraphStore()
