# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""测试专用的固定身份认证器：显式穿过认证边界，不由生产代码自述身份。

历史上这是公共导出的 ``jiuwen_memory.api.ScopedAuthenticator``，复验（P1-2/P2-1）
判定「忽略凭据、接受调用方指定 actor/role 的认证器」是身份自述旁路的改名重建，
已从生产面删除。测试仍需要一个受控身份构造器来布置判定场景——它留在 ``tests``
内，不经 ``AuthProducer`` 注册、不从 ``jiuwen_memory.api`` 导出，装配层守卫
（生产配置拒绝 test-only 认证器）不会被绕过。
"""

from __future__ import annotations

from datetime import datetime, timezone

from jiuwen_memory.common.security.authentication.base import Authenticator
from jiuwen_memory.common.security.types import AuthContext, Credentials, Role
from jiuwen_memory.common.type_def.scope import Scope


class ScopedAuthenticator(Authenticator):
    """返回固定 actor 与 role 的轻量认证器，仅供测试布置身份场景。

    ``delegation_id`` 与 ``credential_id`` 也在这里给，不在调用点用
    ``dataclasses.replace`` 改出来：``RequestSecurityContext._origin`` 的来源绑定已把
    这两个字段纳入 HMAC（见 ``security/types.py`` ``_bind_origin``），换掉任一字段而
    复用旧 ``_origin`` 会被 PEP 判为来源不可信。要布置「调用方声明在代操作」的场景，
    只能让身份构造器一次产出完整 ``AuthContext``——这与生产路径一致：委托声明是认证
    产物，不是调用方事后能改的字段。
    """

    def __init__(
        self,
        actor: Scope,
        *,
        role: Role = Role.USER,
        delegation_id: str = "",
        credential_id: str = "",
    ) -> None:
        self._actor = actor
        self._role = role
        self._delegation_id = delegation_id
        self._credential_id = credential_id

    def authenticate(self, credentials: Credentials) -> AuthContext:
        """忽略凭据，返回构造时指定的 actor 与 role。"""
        del credentials
        return AuthContext(
            actor=Scope(
                org=self._actor.org,
                space=self._actor.space,
                user=self._actor.user,
                agent=self._actor.agent,
                session=self._actor.session,
            ),
            role=self._role,
            credential_type="internal",
            credential_id=self._credential_id,
            auth_method="internal",
            authenticated_at=datetime.now(timezone.utc),
            delegation_id=self._delegation_id,
        )

    @staticmethod
    def mode() -> str:
        return "scoped"

    @staticmethod
    def requires_concurrency_guard() -> bool:
        return False

    @staticmethod
    def health() -> None:
        return None
