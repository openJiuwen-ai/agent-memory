# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``RequestSecurityContext`` 的受控构造入口（F05 §RequestSecurityContext、§进程内调用）。

构造点收在这里，不散在各 surface：``request_id`` 由服务端生成、``started_at`` 取服务端
时钟、``attributes`` 只由系统组件写入——这三条不变量只有一处实现，新增一个接入形态时
不会各自发明一套（迁移计划 §5.2 第 7 项）。

两个入口对应两类调用方：

- :func:`new_request_context` 给**已完成认证**的 surface 用（HTTP / MCP / CLI 经
  ``bootstrap.core.auth_middleware`` 调它）；
- :func:`internal_context` 给**进程内直连**的调用方用（示例脚本、评测 harness、
  嵌入式插件）——它们没有网络对端，但契约与外部请求完全相同（F05 §进程内调用）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from contextvars import ContextVar, Token
from datetime import datetime, timezone

from jiuwen_memory.common.security.types import (
    AuthContext,
    Credentials,
    RequestSecurityContext,
    Surface,
    _bind_origin,
)

_REQUEST_ID: ContextVar[str | None] = ContextVar("agent_memory_request_id", default=None)


def set_request_id(request_id: str) -> Token[str | None]:
    """Set the current request ID for logging and audit correlation only."""
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    """Restore the previous request ID after a request scope exits."""
    _REQUEST_ID.reset(token)


def get_request_id() -> str | None:
    """Return the request ID of the current authenticated request, if any."""
    return _REQUEST_ID.get()


def new_request_context(
    auth: AuthContext,
    *,
    surface: Surface,
    peer: str = "",
    attributes: Mapping[str, str] | None = None,
    request_id: str | None = None,
) -> RequestSecurityContext:
    """把一个**已认证**的 :class:`AuthContext` 包成本次请求的安全上下文。

    未传 ``request_id`` 时在这里生成。受控 HTTP 适配层可传入入口刚生成的 ID，
    让响应、认证上下文与审计记录保持一致；该值不得来自客户端 header、
    query 或业务 payload。它只用于日志与审计关联，不参与 actor、target 或授权判定。

    ``surface`` 无默认值，必须由适配层显式写入——缺省成 ``INTERNAL`` 会让一个漏传的
    HTTP 请求在审计里看起来像进程内调用。

    ``attributes`` 只允许系统组件写入（可信代理链、mTLS 主体一类）。业务 payload 一律
    不得注入：它参与授权环境，可注入就等于把授权输入交给了调用方。

    Round3: _origin 绑定完整 RequestSecurityContext 安全字段，防止 replace(attributes=...) 提权。
    """
    # 先构造上下文（_origin 用占位符）
    context = RequestSecurityContext(
        auth=auth,
        request_id=request_id or uuid.uuid4().hex,
        peer=peer,
        surface=surface,
        started_at=datetime.now(timezone.utc),
        attributes=attributes or {},
        _origin="",  # 占位符
    )
    # Round3: 用完整上下文计算绑定值，然后替换 _origin
    # 使用 object.__setattr__ 绕过 frozen dataclass 限制
    object.__setattr__(context, "_origin", _bind_origin(auth, context))
    return context


def internal_context(authenticator) -> RequestSecurityContext:
    """进程内直连调用方的受控上下文入口（F05 §进程内调用）。

    身份仍**由 authenticator 产出**，不由调用方声明--这正是 F05 拒绝 ``auth=None``
    与「传入 Scope 直接当已认证 actor」的那条线（迁移计划 §5.3）。调用方要操作哪个
    Scope，照旧走业务参数；它决定不了自己是谁。

    ``authenticator`` **必填**：无参领取 ROOT 已不再允许。进程内直连确实信任内核装配
    方，但这份信任必须是一次显式传入（如 ``internal_context(DevAuthenticator())``），
    让「这里拿到了 ROOT」在调用点看得见，而不是像旧的 ``authenticator=None`` 默认那样
    谁都不写就悄悄得到一个超管身份。

    **不可用于有网络对端的场景。** 网络接入必须走 ``auth_middleware.authenticated``：
    那里有真实凭据校验、限流、并发预算和入口审计，这里一样都没有。
    """
    return new_request_context(
        authenticator.authenticate(Credentials()),
        surface=Surface.INTERNAL,
    )
