"""LifecycleManager — 生命周期管理（架构 §3.1）。

管理记忆单元的状态流转：active → superseded（被取代）/ archived（归档）
/ forgotten（遗忘）。一律「标记失效」而非物理删除（非破坏式，保留
provenance 血缘），合规删除等硬删除场景由治理流程显式触发。
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime

from common.type_def import LifecycleState, MemoryUnit, Scope
from common.factory.factory import Factory

from .base import ControlOperator


class LifecycleProducer(Factory):
    """LifecycleManager 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``lifecycle_impl`` 下以 ``@LifecycleProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`control.bootstrap.register_controllers` 统一触发。
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
    def sweep(self) -> list[str]:
        """扫描到期（t_invalid 已过）/低价值的记忆，按策略降权、归档或
        遗忘；返回被处理的记忆单元 id。"""
