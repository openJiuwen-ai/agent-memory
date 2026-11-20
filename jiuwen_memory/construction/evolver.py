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

from jiuwen_memory.common.errors import AgentMemoryError, UnsupportedCapabilityError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import HierarchyKind, MemoryUnit, Modality

from .base import ConstructionOperator
from .hierarchy_composer import (
    HierarchyComposeOptions,
    HierarchyComposeProfile,
    HierarchyComposeResult,
    HierarchyIncrementalContext,
)


class EvolveMode(str, Enum):
    """演进阶段（对应记忆接口 ``evolve(scope, mode)`` 的 mode）。

    仅含**记忆内容**的演进阶段；索引维护不在此列——它随数据面操作
    （write/update/delete）由 IndexBuilder 增量跟进（build/update/remove），
    从真源全量重建则走 IndexBuilder.rebuild() 维护路径，均不作为 evolve 模式。
    """

    EXTRACT = "extract"
    ASSOCIATE = "associate"
    CONSOLIDATE = "consolidate"
    FORGET = "forget"
    HIERARCHY = "hierarchy"
    CONFLICT_CONSOLIDATE = "conflict_consolidate"


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


class ExtractNotCommittedError(AgentMemoryError):
    """本批没有任何派生记忆被提交，可以安全重放（F10 决策 8）。

    只在「抽取阶段」抛出：此时还没有任何派生记忆落盘，调用方可以退回逐条重跑。
    去重或落盘过程中出现的错误**不得**包装成这个类型——那些情况下可能已有部分
    写入，重放会产生重复记忆。
    """


@dataclass
class GroupedExtractResult:
    """合并抽取的产出与来源归属（F10 决策 6）。

    ``sources`` 的 key 是派生记忆最终落盘的 id（UPDATE 沿用目标 id，SUPERSEDE 用新
    版本 id），value 是去重前对应的原始 unit id 列表。归属按**去重前候选**的
    ``provenance`` 计算：UPDATE / SUPERSEDE 回传的对象是已有记忆，其 provenance 不一定
    包含本批来源。
    """

    result: EvolveResult
    sources: dict[str, list[str]] = field(default_factory=dict)


class EvolverProducer(Factory):
    """Evolver 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``evolver_impl`` 下以 ``@EvolverProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "evolver"


class Evolver(ConstructionOperator):
    def structured_modalities(self) -> frozenset[Modality]:
        """Declare structured inputs supported by the EXTRACT path."""
        return frozenset()

    def configure_retention(self, guard) -> None:
        """Inject a trusted reference guard; failure must prevent automatic cleanup."""
        self._retention_guard = guard

    def retains(self, unit: MemoryUnit) -> bool:
        guard = getattr(self, "_retention_guard", None)
        return bool(guard(unit)) if guard is not None else False

    @staticmethod
    def hierarchy_profile(kind: HierarchyKind) -> HierarchyComposeProfile | None:
        """返回绑定 Composer 的配置快照；未声明能力时不启用自动建树。"""
        return None

    @abstractmethod
    def evolve(
        self, units: list[MemoryUnit] | EvolveRequest, mode: EvolveMode | None = None,
    ) -> EvolveResult:
        """对一批记忆单元执行指定阶段的演进，返回变更结果。"""

    def supports_grouped_extract(self) -> bool:
        """是否支持一次抽取整批、并把派生记忆按来源分回各条（F10 决策 5）。

        默认 ``False``：不声明时上层退回逐条抽取，不假定任何实现都能给出逐条来源。
        """
        return False

    def evolve_grouped(self, units: list[MemoryUnit]) -> GroupedExtractResult:
        """一次 EXTRACT 抽取整批，并返回派生记忆的来源归属。

        默认实现明确不支持——调用方必须先查 :meth:`supports_grouped_extract`。
        抽取阶段失败时抛 :class:`ExtractNotCommittedError`（本批没有任何派生记忆落盘，
        可安全重放）；落盘阶段的失败保留原异常。
        """
        raise UnsupportedCapabilityError(
            "grouped_extract",
            type(self).__name__,
            "Evolver",
            f"{type(self).__name__} does not support grouped extract",
        )
