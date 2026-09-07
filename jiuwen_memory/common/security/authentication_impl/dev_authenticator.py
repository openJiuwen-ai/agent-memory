# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""仅用于本地功能测试的固定身份认证器。"""

from __future__ import annotations

from jiuwen_memory.common.security.authentication.base import Authenticator, AuthProducer
from jiuwen_memory.common.security.types import AuthContext, Credentials, Role
from jiuwen_memory.common.type_def.scope import Scope


class DevAuthenticator(Authenticator):
    """忽略凭据并返回服务端固定身份，不得用于生产环境。"""

    def __init__(self, actor: Scope | None = None) -> None:
        source = actor or Scope(org="local", user="developer")
        self._actor = Scope(
            org=source.org,
            space=source.space,
            user=source.user,
            agent=source.agent,
            session=source.session,
        )

    def authenticate(self, credentials: Credentials) -> AuthContext:
        """忽略测试请求携带的凭据，返回具名 ROOT 身份。"""
        del credentials
        return AuthContext(
            actor=Scope(
                org=self._actor.org,
                space=self._actor.space,
                user=self._actor.user,
                agent=self._actor.agent,
                session=self._actor.session,
            ),
            role=Role.ROOT,
            credential_type="dev",
            auth_method="dev",
        )

    @staticmethod
    def mode() -> str:
        return "dev"

    @staticmethod
    def requires_concurrency_guard() -> bool:
        return False

    @staticmethod
    def health() -> None:
        return None


@AuthProducer.register("dev")
def _build(_config) -> DevAuthenticator:
    return DevAuthenticator()
