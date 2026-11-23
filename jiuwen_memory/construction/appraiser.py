# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Appraiser — 写入准入与评价算子（mem2.0 经验记忆，§2.2）。

形式与 ``router.py`` / ``layer_annotator.py`` 相同：契约在这里，实现自注册
（``appraiser_impl`` 下以 ``@AppraiserProducer.register("<名>")`` 注册），profile
不配置即整段跳过——两个 Evolver 的调用点在未取到实例时不接线，与现有的可选装配
同形。

评价算子与 Extractor 的分工：``appraise`` 在抽取之前对本次写入做准入与评价，
允许有副作用（读写专用记录）；``finalize`` 在流程结束时收尾一次。经验 profile
用一个实现承担轨迹沉淀八步中的第 1-5 步与第 8 步：重复检查、结构过滤、判定
反馈、判定成败、归因，与写复现事件、更新计数、轨迹置已完成的收尾。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit

from .base import ConstructionOperator


class AppraiserProducer(Factory):
    """Appraiser 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``appraiser_impl`` 下以 ``@AppraiserProducer.register
    ("<名>")`` 自注册——注册发生在 import 实现模块时，由
    :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "appraiser"


@dataclass
class Appraisal:
    """一次评价的结论。

    ``proceed`` 为假时本次写入到此结束：不抽取、也不调 ``finalize``——轨迹记录的
    状态由 ``appraise`` 自己置好。``data`` 的类型由实现定义：经验的实现是
    ``ExperienceAppraisal``，经抽取上下文（``ExtractContext.appraisal``）传给抽取
    算子，并在 ``finalize`` 时回到评价算子手里。
    """

    proceed: bool
    data: Any = None


class Appraiser(ConstructionOperator):
    """写入准入与评价算子。"""

    @abstractmethod
    def appraise(self, units: list[MemoryUnit]) -> Appraisal:
        """抽取之前调用：对本次写入做准入与评价，允许有副作用（读写记录）。

        抛出异常之前，实现自己写入的状态须已置为失败。
        """

    @abstractmethod
    def finalize(
        self,
        units: list[MemoryUnit],
        appraisal: Appraisal,
        result: Any,
    ) -> None:
        """准入的写入在流程结束时调用一次。

        ``result`` 是流程的落盘结果（``EvolveResult``）；流程抛出异常时以 ``None``
        调用，实现据此把状态置为失败。抛出异常之前，实现自己写入的状态须已置为
        失败。
        """
