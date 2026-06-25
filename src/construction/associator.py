"""Associator — 关联分析（架构 §6.1）。

发现实体共指、因果/引用链、跨会话/跨 Agent 的关联，构成中抽象的
关系/主题结构，支撑多跳推理与「连点成线」；产出的 :class:`Relation`
交由 IndexBuilder 写入图索引。
"""

from __future__ import annotations

from abc import abstractmethod

from common.factory.factory import Factory
from common.type_def import MemoryUnit, Relation

from .base import ConstructionOperator


class AssociatorProducer(Factory):
    """Associator 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``associator_impl`` 下以 ``@AssociatorProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "associator"


class Associator(ConstructionOperator):
    @abstractmethod
    def associate(self, units: list[MemoryUnit]) -> list[Relation]:
        """在一批记忆单元间做关联分析，返回发现的关联关系。"""
