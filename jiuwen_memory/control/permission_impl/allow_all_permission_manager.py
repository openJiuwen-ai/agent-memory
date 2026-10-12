# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~control.permission.PermissionManager`。

最小放行策略（``check`` 恒真），仅记录授权——适合单租户本地装配 / demo。
真实部署应据 scope 包含关系与 Grant 表做实际校验。
"""

from __future__ import annotations

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.permission import PermissionManager, PermissionProducer
from jiuwen_memory.control.types import Action, Grant, PermissionContext


class AllowAllPermissionManager(PermissionManager):
    """最小权限：记录授权但 check 恒放行（适合单租户本地装配）。"""

    def __init__(self) -> None:
        self.grants: list[Grant] = []

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
    ) -> bool:
        return True


# -- 注册到 PermissionProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@PermissionProducer.register("allow_all")
def _build(config):
    return AllowAllPermissionManager()
