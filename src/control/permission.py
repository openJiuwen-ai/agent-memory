"""PermissionManager — 权限管理（架构 §3.2）。

scope 模型的执行点：检索/写入默认限制在自身 scope 内，跨 scope 访问
（多 Agent 共享池等）必须显式授权。授权/回收/校验都产生审计事件。
"""

from __future__ import annotations

from abc import abstractmethod

from common.type_def import Scope

from .base import ControlOperator
from .types import Action, Grant


class PermissionManager(ControlOperator):
    @abstractmethod
    def grant(self, grant: Grant) -> None:
        """新增一条跨 scope 授权。"""

    @abstractmethod
    def revoke(self, grant: Grant) -> None:
        """回收一条授权（幂等）。匹配哪条既有授权（按 grantor+grantee、是否
        逐 action 撤销等）由具体实现定义。"""

    @abstractmethod
    def check(self, actor: Scope, target: Scope, action: Action) -> bool:
        """校验 ``actor`` 是否可对 ``target`` scope 执行 ``action``。"""
