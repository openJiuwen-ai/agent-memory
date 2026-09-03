# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""接口先行过渡期的调用方桥接（**PR2 切 ``security=`` 签名时删除**）。

背景：``MemoryAPI`` 公开签名已从 ``identity: Scope`` 固化为
``security: RequestSecurityContext``（接口契约先行合入），但 ``dispatch`` 与各
进程内调用方的签名仍收 ``Scope``。过渡期内，这些调用点（handler / CLI / MCP /
测试 / 示例）用本函数把手上的 ``Scope`` 包装成 ``RequestSecurityContext`` 继续传入。

**这里的 role / credential 是占位值**：``AuthContext`` 的这些字段为 ``legacy``，
:class:`~api.memory_api_impl.local_memory_api.LocalMemoryAPI` 只取
``security.auth.actor`` 走原有的 PermissionManager 路径，鉴权行为与 identity 直传
时代逐位等价。PR1 未给 `PermissionManager` 加 role 闸门、也不读 ContextVar 透传
role（该鉴权接缝已在 PR1 撤回），`role=Role.ROOT` 在 PR1 无执行点、不产生特权——
role 放行判定随 PR2 由 `Authorizer` 接管。

传进来的 ``actor`` 本身**不再来自 payload**：PR1 的 ``handler._identity()`` 只认
认证中间件产出的 ``AuthContext``，payload 里的 ``actor_*`` 字段一律拒（身份铁律 #1）。
本模块随 PR2 把 ``security=`` 显式签名接到 ``dispatch`` 时与全部调用点一并删除。
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
