# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DEV 认证：无条件返回 ROOT（F05 §Authentication）。

**只用于本地开发。** 配套的 loopback 强制绑定由
:class:`~common.security.protection.binding_policy.BindingPolicy` 在 socket 绑定前
执行——本类不知道服务器绑了哪个地址，也不该在一个可被单测 import 的类里 ``sys.exit``。
它只负责声明 ``requires_loopback_binding()``，由 surface 拿这个 capability 去调策略。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from jiuwen_memory.common.security.authentication.base import Authenticator, AuthProducer
from jiuwen_memory.common.security.types import AuthContext, Credentials, Role
from jiuwen_memory.common.type_def.scope import Scope

_METHOD = "dev"  # 开放字符串而非封闭枚举（F05 拒绝以模式名驱动核心分支）

# 本地开发主体。**不是空 ``Scope()``**：空 Scope 在旧实现里是 platform-admin 的
# 隐式形态，IMPL-01 把那条数据形状巧合断掉了——ROOT 只由 ``role`` 表达，actor 只
# 表达「是谁」。给它一个具名主体，审计里也才看得出这条记录出自 dev 认证。
_DEV_ACTOR = Scope(org="system", user="dev")


class DevAuthenticator(Authenticator):
    """恒 ROOT，不校验任何凭据。"""

    def authenticate(self, credentials: Credentials) -> AuthContext:
        """无条件返回 ROOT 身份。

        权限来自 ``role=Role.ROOT``（服务端角色），不来自 actor 的形状。
        该 ``role`` 在 PR1 没有消费点：``PermissionManager`` 不做 role/actor
        判定，放行语义随 PR2 由 ``Authorizer`` 接管。
        """
        # ``_DEV_ACTOR`` 是模块级共享对象，且 ``Scope`` 为可变 dataclass。
        # 认证结论可能被上层改写 ``actor`` 字段，直接复用会让一次请求的改写
        # 永久污染后续所有认证请求（NEW-SEC-01）。``replace`` 每次生成独立
        # 副本，字段值不变、只断开共享。
        return AuthContext(
            actor=replace(_DEV_ACTOR),
            role=Role.ROOT,
            credential_type=_METHOD,
            auth_method=_METHOD,
            authenticated_at=datetime.now(timezone.utc),
        )

    def mode(self) -> str:
        return _METHOD

    def requires_concurrency_guard(self) -> bool:
        return False

    def health(self) -> None:
        return None


@AuthProducer.register("dev")
def _build(config):
    return DevAuthenticator()
