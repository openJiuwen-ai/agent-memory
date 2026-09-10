# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Evolver — 记忆自演进（架构 §8）。

持续驱动「抽取 → 关联 → 冲突消解 → 升华 → 遗忘/降权」闭环：
新旧矛盾标记失效（非破坏式、保留血缘），近重复记忆融合压缩，
过期/低价值记忆降权或归档。在线（hot path）/离线（background）
双通道由控制层调度，本算子只定义演进动作本身。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import HierarchyKind, MemoryUnit

from .base import ConstructionOperator
from .hierarchy_composer import (
    HierarchyComposeOptions,
    HierarchyComposeProfile,
    HierarchyComposeResult,
    HierarchyIncrementalContext,
)


class EvolveMode(str, Enum):
    """内容与结构演进模式；HIERARCHY 当前仅用于内部构建调用。

    包含记忆内容演进及显式树结构构建；独立索引维护不在此列——它随数据面操作
    （write/update/delete）由 IndexBuilder 增量跟进（build/update/remove），
    从真源全量重建则走 IndexBuilder.rebuild() 维护路径，均不作为 evolve 模式。
    """

    EXTRACT = "extract"
    ASSOCIATE = "associate"
    CONSOLIDATE = "consolidate"
    FORGET = "forget"
    HIERARCHY = "hierarchy"


@dataclass
class EvolveRequest:
    """一次演进的输入；metadata 仅作请求上下文，不写回记忆。"""

    units: list[MemoryUnit]
    mode: EvolveMode
    metadata: dict[str, str] = field(default_factory=dict)
    hierarchy_options: HierarchyComposeOptions | None = None
    hierarchy_incremental: HierarchyIncrementalContext | None = None


@dataclass
class EvolveResult:
    """一次演进的产出：新增/更新/被取代/被遗忘的记忆单元 id，以及落盘产物本身。

    ``created_units`` 回传实际落盘的对象。归属判定改写派生单元的 scope 之后，调用方按
    原 scope 回读真源会落空——只有回传对象才取得到。新增与版本替换两条分支都要回填：
    前者是 ``_persist``，后者是去重判定为 UPDATE / SUPERSEDE 时写入的新版本。
    """

    created_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)
    superseded_ids: list[str] = field(default_factory=list)
    forgotten_ids: list[str] = field(default_factory=list)
    created_units: list[MemoryUnit] = field(default_factory=list)
    hierarchy_result: HierarchyComposeResult | None = None


class EvolverProducer(Factory):
    """Evolver 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``evolver_impl`` 下以 ``@EvolverProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "evolver"


class Evolver(ConstructionOperator):
    @staticmethod
    def hierarchy_profile(kind: HierarchyKind) -> HierarchyComposeProfile | None:
        """返回本 Evolver 绑定 Composer 的显式 profile，缺省不启用增量建树。"""
        return None

    @abstractmethod
    def evolve(self, request: EvolveRequest) -> EvolveResult:
        """按请求执行指定模式的演进，返回内容或结构变更结果。"""
