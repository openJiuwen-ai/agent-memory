# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Classifier — 多维分类（架构 §6.1）。

按主题/认知角色/来源/重要度等多维度归类：认知角色（tier）决定记忆
常驻上下文还是按需检索；标签写入 ``tags``/``metadata`` 供检索前置过滤。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit

from .base import ConstructionOperator


class ClassifierProducer(Factory):
    """Classifier 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``classifier_impl`` 下以 ``@ClassifierProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "classifier"


class Classifier(ConstructionOperator):
    @abstractmethod
    def classify(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """为一批记忆单元打上 tier/主题/重要度等分类标签，返回更新后的单元。"""
