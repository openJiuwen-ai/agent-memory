"""GraphStore — 属性图存储，节点与边统一 CRUD + 邻域遍历。

增/删/改在一次调用内同时作用于节点与边（任一列表可为空），
保持与其他存储一致的四动词形态。``scope`` 为显式第一入参：写入按 ``scope``
落库，``search`` 遍历 / 按 id 的 ``get`` / ``delete`` 物理约束在该 ``scope`` 内。
``id`` 为 scope 内逻辑主键。
"""

from __future__ import annotations

from abc import abstractmethod

from common.factory.factory import Factory
from common.type_def import Scope

from .base import BaseStore
from .types import Edge, GraphQuery, Node


class GraphProducer(Factory):
    """GraphStore 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即后端名（如 memory / nano_graphrag）。各实现在 ``graph_impl`` 下以
    ``@GraphProducer.register("<后端>")`` 自注册——注册发生在 import 实现模块时，由
    :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "graph_store"


class GraphStore(BaseStore):
    @abstractmethod
    def seed_ids(self, scope: Scope, tokens: set[str]) -> list[str]:
        """
        在 ``scope`` 内按关键词/词项定位**种子节点 id**：图召回路在
        query 不携带显式起点时，先据此找到入口节点，再以其为 ``start_id``
        调 :meth:`search` 多跳扩展。匹配语义（节点哪些属性、是否子串/词项）
        由后端定义；``tokens`` 为空时返回空。后端可用其原生查询能力实现
        （如 Neo4j 全文索引、Cypher）。
        """

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
