# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Abstractor — 抽象与精炼/升华（架构 §6.1）。

在低/中抽象记忆之上做概括：情景→语义、经验→技能/模式，升华出画像、
长期偏好、可复用技能等**高抽象粒度**记忆。产物通过 ``provenance``
记录血缘（由哪些 unit 升华而来），保证可重建、可审计回溯。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit

from .base import ConstructionOperator


class AbstractorProducer(Factory):
    """Abstractor 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``abstractor_impl`` 下
    以 ``@AbstractorProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "abstractor"


class Abstractor(ConstructionOperator):
    # 产物是否先与落点空间已有条目语义去重再落盘（mem2.0 §2.5.3）。缺省
    # 去重（事实/画像类产物语义相同即重复）。技能类产物置为假：同一技能
    # 的新版与旧版内容天然相近，语义去重会把新版误判成重复——技能的新旧
    # 版本靠条目 id 幂等与版本号区分（skill_abstractor），不走语义去重；
    # Evolver 读本属性决定落盘前是否过 _dedup_batch。
    dedup_products: bool = True

    @abstractmethod
    def abstract(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """对一批记忆单元做抽象与精炼，产出高抽象粒度的新记忆单元。"""
