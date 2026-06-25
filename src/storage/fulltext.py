"""FulltextStore — 全文倒排索引存储，统一 CRUD + 关键词检索。

``scope`` 为显式第一入参：写入按 ``scope`` 落库，``search`` / 按 id 的 ``get`` /
``delete`` 物理约束在该 ``scope`` 内。``id`` 为全局唯一主键。
"""

from __future__ import annotations

from abc import abstractmethod

from common.factory.factory import Factory
from common.type_def import Scope

from .base import BaseStore
from .types import Document, ScoredID, TextQuery


class FulltextProducer(Factory):
    """FulltextStore 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即后端名（如 memory / elasticsearch）。各实现在 ``fulltext_impl`` 下以
    ``@FulltextProducer.register("<后端>")`` 自注册——注册发生在 import 实现模块时，由
    :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "fulltext_store"


class FulltextStore(BaseStore):
    @abstractmethod
    def insert(self, scope: Scope, docs: list[Document]) -> None:
        """在 ``scope`` 下索引新文档；id 已存在时报冲突。"""

    @abstractmethod
    def update(self, scope: Scope, docs: list[Document]) -> None:
        """重建已有文档的索引；id 不存在时报缺失。"""

    @abstractmethod
    def delete(self, scope: Scope, ids: list[str]) -> None:
        """在 ``scope`` 内按 id 删除文档（幂等）。"""

    @abstractmethod
    def get(self, scope: Scope, ids: list[str]) -> list[Document]:
        """在 ``scope`` 内按 id 点查文档；缺失/越界的 id 从结果中省略。"""

    @abstractmethod
    def search(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        """在 ``scope`` 内做关键词检索（BM25 等），按相关性返回 top-k。"""
