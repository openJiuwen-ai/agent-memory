"""VectorStore — 向量存储，统一 CRUD + ANN 检索。

``scope`` 为显式第一入参：写入按 ``scope`` 落库，``search`` / 按 id 的 ``get`` /
``delete`` 物理约束在该 ``scope`` 内。``id`` 为全局唯一主键。
"""

from __future__ import annotations

from abc import abstractmethod

from common.factory.factory import Factory
from common.type_def import Scope

from .base import BaseStore
from .types import ScoredID, VectorQuery, VectorRecord


class VectorProducer(Factory):
    """VectorStore 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即后端名（如 memory / milvus）。各实现在 ``vector_impl`` 下以
    ``@VectorProducer.register("<后端>")`` 自注册——注册发生在 import 实现模块时，由
    :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "vector_store"


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

    def score_higher_is_better(self) -> bool:
        """``search`` 返回分数的方向语义：True=分越大越相关（cosine/IP 类）。

        召回层的 chunk→unit MaxP、分层归并和融合排序统一要求高分优先。
        距离型后端（如 L2，越小越相关）**必须** override 返回 False，
        ``VectorRecaller`` 会在装配期拒绝该后端，避免静默反转相关性。默认 True。
        """
        return True
