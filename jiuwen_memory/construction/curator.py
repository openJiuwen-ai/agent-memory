# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Curator — 周期整理（mem2.0 §2.5.2）。

周期任务以 ``CONSOLIDATE`` 模式调用经验 profile 的 evolver 时，在升华之前
先经本算子整理：聚合效用事件、退闲置与有害条目、合并重复、淘汰竞争组中
较差的、控制各类别条目数、覆写类别计数。整理排在归纳之前——归纳只用整理
后留下的条目，并用到整理第 1 步刚聚合出的统计记录。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit

from .base import ConstructionOperator


class CuratorProducer(Factory):
    """Curator 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``curator_impl`` 下以 ``@CuratorProducer.register("<名>")``
    自注册——注册发生在 import 实现模块时，由 :func:`construction.bootstrap.register_constructors`
    统一触发。
    """

    TOP_NAME = "curator"


class Curator(ConstructionOperator):
    @abstractmethod
    def curate(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """整理一批经验条目，返回留下的条目（交给升华）。

        有一步失败时返回空列表，本轮不归纳（失败的步骤与原因记入运行摘要）。
        副作用：每条条目一份统计记录与类别基线写回经验空间，被退休与被合并
        的条目改状态，写一条运行摘要。
        """
