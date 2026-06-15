"""VectorStore — 向量存储，统一 CRUD + ANN 检索。

``scope`` 为显式第一入参：写入按 ``scope`` 落库，``search`` / 按 id 的 ``get`` /
``delete`` 物理约束在该 ``scope`` 内。``id`` 为全局唯一主键。
"""

from __future__ import annotations

from abc import abstractmethod

from common.type_def import Scope

from .base import BaseStore
from .types import ScoredID, VectorQuery, VectorRecord


class VectorStore(BaseStore):
    @abstractmethod
    def insert(self, scope: Scope, records: list[VectorRecord]) -> None:
        """在 ``scope`` 下新建向量行；id 已存在时报冲突。"""

    @abstractmethod
    def update(self, scope: Scope, records: list[VectorRecord]) -> None:
        """替换已有向量行；id 不存在时报缺失。"""

    @abstractmethod
    def delete(self, scope: Scope, ids: list[str]) -> None:
        """在 ``scope`` 内按 id 删除向量行（幂等）。"""

    @abstractmethod
    def get(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        """在 ``scope`` 内按 id 点查向量行；缺失/越界的 id 从结果中省略。"""

    @abstractmethod
    def search(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        """在 ``scope`` 内做 ANN 近邻检索，按相似度返回 top-k。"""
