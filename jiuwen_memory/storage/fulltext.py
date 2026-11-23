# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""FulltextStore — 全文倒排索引存储，统一 CRUD + 关键词检索。

``scope`` 为显式第一入参：写入按 ``scope`` 落库，``search`` / 按 id 的 ``get`` /
``delete`` 物理约束在该 ``scope`` 内。``id`` 为 scope 内逻辑主键。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope

from .base import BaseStore
from .types import Document, ScoredHit, ScoredID, TextQuery


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

    def recall(
        self,
        scope: Scope,
        query: TextQuery,
        output_fields: list[str] | None = None,
    ) -> list[ScoredHit]:
        """按 ``search`` 的 scope、过滤、扩展和排序语义召回，并在同次请求回带 payload。

        :param output_fields: 当前仅认 ``"metadata"``，请求时必须返回命中记录的
            完整 metadata（含自定义键、``unit_id`` 与 ``entities``），不得仅投影部分键。
            其余字段忽略并记日志；``None`` 或空列表不回带，metadata 为空 dict。
        :return: 按相关性降序排列的命中，id/score 与 ``search`` 同口径。
        :raises NotImplementedError: 可选能力，基类默认抛出；调用方仅捕获此异常，
            回退到 ``search`` + ``get``。真实后端错误不得触发回退。
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement recall (search with payload); "
            "falling back to search + get"
        )
