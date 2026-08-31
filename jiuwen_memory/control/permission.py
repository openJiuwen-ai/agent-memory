# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""PermissionManager — 权限管理（架构 §3.2）。

scope 模型的执行点：检索/写入默认限制在自身 scope 内，跨 scope 访问
（多 Agent 共享池等）必须显式授权。授权/回收/校验都产生审计事件。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.authorization.base import RoutingFieldsProvider
from jiuwen_memory.common.security.space_decision import DecisionOutcome
from jiuwen_memory.common.security.types import Action, Grant
from jiuwen_memory.common.type_def import Scope

from .base import ControlOperator
from .types import PermissionContext


class PermissionProducer(Factory):
    """PermissionManager 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``permission_impl`` 下以
    ``@PermissionProducer.register("<名>")`` 自注册——注册发生在 import 实现模块时，
    由 :func:`control.bootstrap.register_controllers` 统一触发。
    """

    TOP_NAME = "permission"


class PermissionManager(ControlOperator, RoutingFieldsProvider):
    @abstractmethod
    def grant(self, grant: Grant) -> None:
        """新增一条跨 scope 授权。"""

    @abstractmethod
    def revoke(self, grant: Grant) -> None:
        """回收一条授权（幂等）。匹配哪条既有授权（按 grantor+grantee、是否
        逐 action 撤销等）由具体实现定义。
        """

    @abstractmethod
    def check(
        self,
        actor: Scope,
        target: Scope,
        action: Action,
        context: PermissionContext | None = None,
    ) -> bool:
        """校验 ``actor`` 是否可对 ``target`` scope 执行 ``action``。"""

    def decide(
        self,
        actor: Scope,
        target: Scope,
        action: Action,
        context: PermissionContext | None = None,
    ) -> DecisionOutcome:
        """完整判定结论：除放行与否外，还带判据名与通过的轴。

        鉴权点的空间策略裁剪要知道通过的是哪条轴（经治理轴通过才可读策略），而布尔
        :meth:`check` 表达不了。裁剪不另发起一次判定，因此结论须由同一次判定带出。

        默认实现折算自 :meth:`check`，轴留空——不做空间级判定的实现无须改动，轴为空
        即「无轴概念」，裁剪随之不执行。

        本方法随 :class:`PermissionManager` 整体退出：安全横切契约合入后判定移入 ``Authorizer``，
        其 ``authorize`` 本就返回带判据的结论对象。
        """
        return DecisionOutcome(
            allowed=self.check(actor, target, action, context),
            rule="permission_manager_check",
        )

    def requires_space_facts(self) -> bool:
        """本实现的判定是否需要鉴权点下发空间授权事实（默认不需要）。

        判定实现不访问存储，空间级判据所需的成员表与归属登记由鉴权点一次读取后随
        :class:`PermissionContext` 传入。该读取有存储成本，因此由实现声明是否需要——
        不做空间级判定的实现返回假，鉴权点即跳过读取，行为与改造前一致。
        """
        return False
