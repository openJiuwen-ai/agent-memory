"""GraphStore — 属性图存储，节点与边统一 CRUD + 邻域遍历。

增/删/改在一次调用内同时作用于节点与边（任一列表可为空），
保持与其他存储一致的四动词形态。``scope`` 为显式第一入参：写入按 ``scope``
落库，``search`` 遍历 / 按 id 的 ``get`` / ``delete`` 物理约束在该 ``scope`` 内。
``id`` 为全局唯一主键。
"""

from __future__ import annotations

from abc import abstractmethod

from common.type_def import Scope

from .base import BaseStore
from .types import Edge, GraphQuery, Node


class GraphStore(BaseStore):
    @abstractmethod
    def insert(
        self,
        scope: Scope,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        """在 ``scope`` 下新建节点/边；id 已存在时报冲突。"""

    @abstractmethod
    def update(
        self,
        scope: Scope,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        """更新已有节点/边；id 不存在时报缺失。"""

    @abstractmethod
    def delete(
        self,
        scope: Scope,
        node_ids: list[str] | None = None,
        edge_ids: list[str] | None = None,
    ) -> None:
        """在 ``scope`` 内按 id 删除节点（连带其关联边）/ 边（幂等）。"""

    @abstractmethod
    def get(self, scope: Scope, node_ids: list[str]) -> list[Node]:
        """在 ``scope`` 内按 id 点查节点；缺失的 id 从结果中省略。"""

    @abstractmethod
    def search(self, scope: Scope, query: GraphQuery) -> list[Node]:
        """在 ``scope`` 内从 ``query.start_id`` 出发扩展邻域/子图（多跳遍历）。"""
