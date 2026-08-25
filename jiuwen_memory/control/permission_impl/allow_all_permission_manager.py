"""最小实现：:class:`~control.permission.PermissionManager`。

最小放行策略（``check`` 恒真），仅记录授权——适合单租户本地装配 / demo。
真实部署应据 scope 包含关系与 Grant 表做实际校验。
"""

from __future__ import annotations

from typing import List

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.permission import PermissionManager, PermissionProducer
from jiuwen_memory.control.types import Action, Grant, PermissionContext


class AllowAllPermissionManager(PermissionManager):
    """最小权限：记录授权但 check 恒放行（适合单租户本地装配）。"""

    def __init__(self) -> None:
        """初始化 AllowAllPermissionManager。"""
        self.grants: List[Grant] = []

    def operator_type(self) -> ControlOperatorType:
        """返回当前算子类型。

        Returns:
            返回 ControlOperatorType。
        """
        return ControlOperatorType.PERMISSION

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def grant(self, grant: Grant) -> None:
        """执行 `grant` 操作。

        Args:
            grant: 参数 grant（Grant）。
        """
        self.grants.append(grant)

    def revoke(self, grant: Grant) -> None:
        """执行 `revoke` 操作。

        Args:
            grant: 参数 grant（Grant）。
        """
        self.grants = [
            g
            for g in self.grants
            if not (g.grantor == grant.grantor and g.grantee == grant.grantee)
        ]

    def check(
        self,
        actor: Scope,
        target: Scope,
        action: Action,
        context: PermissionContext | None = None,
    ) -> bool:
        """执行 `check` 操作。

        Args:
            actor: 参数 actor（Scope）。
            target: 参数 target（Scope）。
            action: 参数 action（Action）。
            context: 参数 context（PermissionContext | None）。

        Returns:
            返回 bool。
        """
        return True


# -- 注册到 PermissionProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@PermissionProducer.register("allow_all")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return AllowAllPermissionManager()
