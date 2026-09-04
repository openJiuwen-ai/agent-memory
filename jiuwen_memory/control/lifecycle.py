# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""LifecycleManager — 生命周期管理（架构 §3.1）。

管理记忆单元的状态流转：active → superseded（被取代）/ archived（归档）
/ forgotten（遗忘）。一律「标记失效」而非物理删除（非破坏式，保留
provenance 血缘），合规删除等硬删除场景由治理流程显式触发。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Scope

from .base import ControlOperator


@dataclass
class SweepTransition:
    """一次到期清扫的待执行流转（``sweep`` 纯计算产物）。

    只描述"哪个 scope 的哪个单元应从什么状态流转到什么状态"，不修改
    真源、不触碰检索索引——执行（索引清理 + 真源回写）由编排者
    （Engine/Governance）决定。``unit`` 为扫描时的单元快照，供编排者
    直接做 ``IndexBuilder.remove``，避免二次读取真源。
    """

    scope: Scope
    unit_id: str
    from_state: LifecycleState
    to_state: LifecycleState
    unit: MemoryUnit


class LifecycleProducer(Factory):
    """LifecycleManager 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``lifecycle_impl`` 下以
    ``@LifecycleProducer.register("<名>")`` 自注册——注册发生在 import 实现模块时，
    由 :func:`control.bootstrap.register_controllers` 统一触发。
    """

    TOP_NAME = "lifecycle"


class LifecycleManager(ControlOperator):
    @abstractmethod
    def transition(
        self, scope: Scope, unit_ids: list[str], target: LifecycleState
    ) -> None:
        """将一批记忆单元流转到目标状态（非破坏式标记）。"""

    @abstractmethod
    def supersede(self, scope: Scope, unit_id: str, invalid_at: datetime) -> MemoryUnit:
        """Mark an old version as superseded and set its valid-time invalid boundary."""

    @abstractmethod
    def sweep(self) -> list[SweepTransition]:
        """纯计算：扫描到期（``t_invalid`` 已过）的 active 单元与 superseded
        旧版本，按策略给出目标态，返回待执行 transition（带 Scope、
        unit_id、from/to state）；不修改真源、不触碰检索索引。

        执行由编排者（Engine/Governance）完成：对 FORGOTTEN 目标先
        ``IndexBuilder.remove(mode=SOFT)`` 移出检索索引，成功后回写真源
        lifecycle；失败组保持原状态，下轮 sweep 重新发现（remove 幂等，
        重试自愈）。
        """
