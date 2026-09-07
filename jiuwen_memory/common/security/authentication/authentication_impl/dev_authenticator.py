# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""仅用于本地功能测试的固定身份认证器。"""

from __future__ import annotations

from datetime import datetime, timezone

from jiuwen_memory.common.security.authentication.base import Authenticator, AuthProducer
from jiuwen_memory.common.security.types import AuthContext, Credentials, Role
from jiuwen_memory.common.type_def.scope import Scope

_DEV_ACTOR = Scope(org="local", user="developer")


class DevAuthenticator(Authenticator):
    """忽略凭据并返回服务端固定身份，不得用于生产环境。"""

    def authenticate(self, credentials: Credentials) -> AuthContext:
        """忽略测试请求携带的凭据，返回具名 ROOT 身份。"""
        del credentials
        return AuthContext(
            actor=Scope(
                org=_DEV_ACTOR.org,
                space=_DEV_ACTOR.space,
                user=_DEV_ACTOR.user,
                agent=_DEV_ACTOR.agent,
                session=_DEV_ACTOR.session,
            ),
            role=Role.ROOT,
            credential_type="dev",
            auth_method="dev",
            authenticated_at=datetime.now(timezone.utc),
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


__all__ = ["DevAuthenticator"]
