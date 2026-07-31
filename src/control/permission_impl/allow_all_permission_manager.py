"""最小实现：:class:`~control.permission.PermissionManager`。

最小放行策略（``check`` 恒真），仅记录授权——适合单租户本地装配 / demo。
真实部署应据 scope 包含关系与 Grant 表做实际校验。
"""

from __future__ import annotations

from typing import List

from common.type_def import Scope
from common.type_def.auth import AuthContext
from control.base import ControlOperatorType
from control.permission import PermissionManager, PermissionProducer
from control.types import Action, Grant, PermissionContext


class AllowAllPermissionManager(PermissionManager):
    """最小权限：记录授权但 check 恒放行（适合单租户本地装配）。"""

    def __init__(self) -> None:
        self.grants: List[Grant] = []

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.PERMISSION

    def health(self) -> None:
        return None

    def grant(self, grant: Grant) -> None:
        self.grants.append(grant)

    def revoke(self, grant: Grant) -> None:
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
        *,
        auth: AuthContext | None = None,
    ) -> bool:
        # 恒放行是本实现的**全部**语义，``auth`` 一并忽略：它是 dev-only 的装配件，
        # 掺进角色闸门只会让「allow_all 就是不鉴权」这个前提变得需要逐条确认。
        return True


# -- 注册到 PermissionProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@PermissionProducer.register("allow_all")
def _build(config):
    return AllowAllPermissionManager()
