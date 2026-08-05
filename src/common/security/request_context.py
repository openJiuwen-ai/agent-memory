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
from datetime import datetime, timezone

from common.security.types import (
    AuthContext,
    Credentials,
    RequestSecurityContext,
    Surface,
)


def new_request_context(
    auth: AuthContext,
    *,
    surface: Surface,
    peer: str = "",
    attributes: Mapping[str, str] | None = None,
) -> RequestSecurityContext:
    """把一个**已认证**的 :class:`AuthContext` 包成本次请求的安全上下文。

    ``request_id`` 在这里生成，**不接受调用方传入**：它进审计与 ``AuthorizationEnvironment``，
    能被调用方指定就等于让调用方给自己的行为贴任意标签、或与他人的记录撞号。

    ``surface`` 无默认值，必须由适配层显式写入——缺省成 ``INTERNAL`` 会让一个漏传的
    HTTP 请求在审计里看起来像进程内调用。

    ``attributes`` 只允许系统组件写入（可信代理链、mTLS 主体一类）。业务 payload 一律
    不得注入：它参与授权环境，可注入就等于把授权输入交给了调用方。
    """
    return RequestSecurityContext(
        auth=auth,
        request_id=uuid.uuid4().hex,
        peer=peer,
        surface=surface,
        started_at=datetime.now(timezone.utc),
        attributes=attributes or {},
    )


def internal_context(authenticator=None) -> RequestSecurityContext:
    """进程内直连调用方的受控上下文入口（F05 §进程内调用）。

    身份仍**由 authenticator 产出**，不由调用方声明——这正是 F05 拒绝 ``auth=None``
    与「传入 Scope 直接当已认证 actor」的那条线（迁移计划 §5.3）。调用方要操作哪个
    Scope，照旧走业务参数；它决定不了自己是谁。

    ``authenticator`` 缺省是 ``DevAuthenticator``（恒 ROOT，具名主体 ``system/dev``）。
    这与进程内直连的实际信任模型一致：调用方**在同一个进程里自己装配了内核**，能直接
    碰 KV 与 Engine，授权拦不住也不该假装拦得住。把它写成显式的一次调用，是为了让这
    份信任在代码里看得见——而不是像旧的 ``auth=None`` 那样，散落在每个调用点上。

    **不可用于有网络对端的场景。** 网络接入必须走 ``auth_middleware.authenticated``：
    那里有真实凭据校验、限流、并发预算和入口审计，这里一样都没有。要用别的身份就把
    装配好的 authenticator 传进来（例如 service credential 的 ``api_key`` 实现）。
    """
    if authenticator is None:
        # 延迟 import：本模块被 surface 与进程内调用方共用，顶层拉 authentication_impl
        # 会让只用 new_request_context 的路径也付一次实现包的 import 代价。
        from common.security.authentication.authentication_impl.dev_authenticator import (
            DevAuthenticator,
        )

        authenticator = DevAuthenticator()
    return new_request_context(
        authenticator.authenticate(Credentials()),
        surface=Surface.INTERNAL,
    )
