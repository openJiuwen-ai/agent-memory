"""最小实现：:class:`~storage.graph.GraphStore` 的纯内存属性图。

按 scope 隔离地存节点/边，``search`` 从 ``start_id`` BFS 扩展邻域（按 depth 跳数、
可选 relation 过滤，边按无向遍历）。无外部图库依赖。

``seed_ids``（GraphStore 契约方法）：按关键词在节点 ``content`` 属性里找种子
节点——供图召回路在「无 query 起点」时定位起点（契约的 ``search`` 要求
``start_id``，而 query 侧并不携带）。本实现以「属性子串命中」匹配。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.graph import GraphProducer, GraphStore
from jiuwen_memory.storage.types import Edge, GraphQuery, Node

_ScopeKey = Tuple[str, str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    """执行 `skey` 操作。

    Args:
        scope: 参数 scope（Scope）。

    Returns:
        返回 _ScopeKey。
    """
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


class InMemoryGraphStore(GraphStore):
    """纯内存属性图：节点/边按 scope 隔离，BFS 邻域遍历。"""

    def __init__(self) -> None:
        """初始化 InMemoryGraphStore。"""
        self._nodes: Dict[_ScopeKey, Dict[str, Node]] = defaultdict(dict)
        self._edges: Dict[_ScopeKey, Dict[str, Edge]] = defaultdict(dict)

    def store_type(self) -> StoreType:
        """返回当前存储类型。

        Returns:
            返回 StoreType。
        """
        return StoreType.GRAPH

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def insert(
        self,
        scope: Scope,
        nodes: Optional[List[Node]] = None,
        edges: Optional[List[Edge]] = None,
    ) -> None:
        """插入一条或多条记录。

        Args:
            scope: 参数 scope（Scope）。
            nodes: 参数 nodes（Optional[List[Node]]）。
            edges: 参数 edges（Optional[List[Edge]]）。

        Raises:
            ConflictError: 执行失败时抛出。
        """
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
        nodes: Optional[List[Node]] = None,
        edges: Optional[List[Edge]] = None,
    ) -> None:
        """更新已有记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            nodes: 参数 nodes（Optional[List[Node]]）。
            edges: 参数 edges（Optional[List[Edge]]）。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
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
        node_ids: Optional[List[str]] = None,
        edge_ids: Optional[List[str]] = None,
    ) -> None:
        """删除指定的记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            node_ids: 参数 node_ids（Optional[List[str]]）。
            edge_ids: 参数 edge_ids（Optional[List[str]]）。
        """
        sk = _skey(scope)
        for nid in node_ids or []:
            self._nodes[sk].pop(nid, None)
            for eid in [e.id for e in self._edges[sk].values() if e.source == nid or e.target == nid]:
                self._edges[sk].pop(eid, None)  # 连带删关联边
        for eid in edge_ids or []:
            self._edges[sk].pop(eid, None)

    def get(self, scope: Scope, node_ids: List[str]) -> List[Node]:
        """读取指定的记录或资源。

        Args:
            scope: 参数 scope（Scope）。
            node_ids: 参数 node_ids（List[str]）。

        Returns:
            返回 List[Node]。
        """
        bucket = self._nodes[_skey(scope)]
        return [bucket[i] for i in node_ids if i in bucket]

    def search(self, scope: Scope, query: GraphQuery) -> List[Node]:
        """检索与查询匹配的结果。

        Args:
            scope: 参数 scope（Scope）。
            query: 参数 query（GraphQuery）。

        Returns:
            返回 List[Node]。
        """
        sk = _skey(scope)
        adj: Dict[str, List[str]] = defaultdict(list)
        for e in self._edges[sk].values():
            if query.relation and e.relation != query.relation:
                continue
            adj[e.source].append(e.target)  # 无向遍历
            adj[e.target].append(e.source)
        visited: Set[str] = {query.start_id}
        frontier = [query.start_id]
        reached: List[str] = []
        for _ in range(max(query.depth, 0)):
            nxt: List[str] = []
            for nid in frontier:
                for nb in adj.get(nid, []):
                    if nb not in visited:
                        visited.add(nb)
                        nxt.append(nb)
                        reached.append(nb)
            frontier = nxt
        nodes = [self._nodes[sk][i] for i in reached if i in self._nodes[sk]]
        return nodes[: query.limit]

    # -- GraphStore 契约：按关键词找种子节点（属性子串命中） ------------------ #
    def seed_ids(self, scope: Scope, tokens: Set[str]) -> List[str]:
        """返回可用于检索的种子标识。

        Args:
            scope: 参数 scope（Scope）。
            tokens: 参数 tokens（Set[str]）。

        Returns:
            返回 List[str]。
        """
        if not tokens:
            return []
        out: List[str] = []
        for nid, node in self._nodes[_skey(scope)].items():
            content = str(node.properties.get("content", ""))
            if any(tok and tok in content for tok in tokens):
                out.append(nid)
        return out


# -- 注册到 GraphProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@GraphProducer.register("memory")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return InMemoryGraphStore()
