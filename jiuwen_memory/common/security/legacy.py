"""PR2 接入前的调用方桥接（**PR2 切 ``security=`` 签名时删除**）。

背景：``MemoryAPI`` 公开签名已从 ``identity: Scope`` 固化为
``security: RequestSecurityContext``，但 MCP 与历史进程内调用仍持有 ``Scope``。
PR1 已实装 dev/trusted/api_key 认证；正式角色授权仍由 PR2 Authorizer 接通。
过渡期调用点用本函数包装原有 identity。

这里的 role / credential 字段是 legacy 占位；PR1 的 PermissionManager 只消费 actor，
不会把占位 ROOT 当成特权。HTTP/CLI 的业务 payload 不得声明 actor；该桥仅保留给受控
适配层，随 PR2 显式安全上下文接线一并删除。
"""

from __future__ import annotations

from jiuwen_memory.common.security.request_context import new_request_context
from jiuwen_memory.common.security.types import AuthContext, RequestSecurityContext, Surface
from jiuwen_memory.common.type_def.scope import Scope


def legacy_request_context(
    actor: Scope,
    *,
    surface: Surface = Surface.INTERNAL,
    peer: str = "",
) -> RequestSecurityContext:
    """把旧调用方的 identity ``Scope`` 包装成 ``RequestSecurityContext``。"""
    return new_request_context(
        AuthContext(
            actor=actor,
            credential_type="legacy",
            auth_method="legacy",
        ),
        surface=surface,
        peer=peer,
    )
