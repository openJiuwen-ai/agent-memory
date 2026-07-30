"""请求作用域的认证上下文——凭据提取 + ContextVar 建立/清理。

各 surface（HTTP / MCP / CLI 直连）用同一条中间件：把本形态的凭据材料归一成
:class:`~security.types.Credentials`，交给装配好的 ``Authenticator``，把产出的
``AuthContext`` 挂进 ContextVar 供 ``handler.dispatch`` 读取。

**本模块不决定认证策略**——模式（dev / trusted / api_key）由配置在装配期选定，
这里只负责「在正确的时机调用它、并保证退出时清理干净」。
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from importlib import import_module
from typing import Any, Iterator, Mapping

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"
)
if _SRC not in sys.path:
    sys.path.append(_SRC)

_auth_module = import_module("common.type_def.auth")
reset_current = _auth_module.reset_current
set_current = _auth_module.set_current

Scope = import_module("common.type_def").Scope
AuditEvent = import_module("common.type_def").AuditEvent
_errors = import_module("common.errors")
AuthenticationError = _errors.AuthenticationError
RateLimitedError = _errors.RateLimitedError
Credentials = import_module("security.types").Credentials

_BEARER = "bearer "

_RATE_LIMITED = "too many requests"


def credentials_from_headers(headers: Mapping[str, Any], peer_address: str = "") -> Credentials:
    """从 HTTP header 提取凭据。

    HTTP header 名大小写不敏感（RFC 9110 §5.1）。``http.client.HTTPMessage`` 的
    ``get`` 自己会做不敏感匹配，但传给 authenticator 的是普通 Mapping——故在这里
    统一归一成小写键，authenticator 侧按小写常量查，两边不必各写一次 ``.lower()``。
    """
    normalized = {str(k).lower(): str(v) for k, v in headers.items()}

    api_key = ""
    auth = normalized.get("authorization", "")
    if auth[: len(_BEARER)].lower() == _BEARER:
        api_key = auth[len(_BEARER) :].strip()
    if not api_key:
        api_key = normalized.get("x-api-key", "").strip()

    return Credentials(api_key=api_key, headers=normalized, peer_address=peer_address)


@contextmanager
def authenticated(
    authenticator, credentials, audit=None, limiter=None, *, argon2_guard=None
) -> Iterator[Any]:
    """在请求作用域内建立可信认证上下文；退出时**必定** reset。

    reset 放 ``finally`` 是硬性要求：``ThreadingHTTPServer`` 每请求一线程，
    但线程可能被池化复用；漏 reset 会让下一个请求继承上一个请求的身份——
    最严重的一类越权。

    ``authenticate`` 故意放在 ``try`` 之外：认证失败时没有 token 可 reset，
    放进 try 会需要一个 ``token = None`` 的分支判断，反而更容易写错。

    ``limiter`` 在 ``authenticate`` **之前**执行（§8.1）：认证本身就是要保护的
    资源——API_KEY 模式下每次 authenticate 跑一次 Argon2id verify（128 MiB ×
    time_cost=4），放在认证之后限流就等于「先让攻击者把 CPU 用掉，再告诉他
    超限了」。``limiter=None`` 表示不限流（进程内直连 / MCP stdio 无网络对端）。

    ``argon2_guard`` 是进程级并发上限（审计 P1-3）：IP 桶限请求速率，限不住
    「同时在跑的 Argon2 verify 数」。耗尽即 429，在 limiter 之后、authenticate
    之前执行。acquire 成功后用 ``finally`` 释放；``None`` 表示不限（DEV 模式或
    调用方确信不跑 Argon2）。
    """
    if limiter is not None and not limiter.allow(credentials.peer_address):
        _record_denial(audit, authenticator, credentials, "rate_limit")
        raise RateLimitedError(_RATE_LIMITED)

    guard_acquired = False
    if argon2_guard is not None:
        if not argon2_guard.acquire():
            _record_denial(audit, authenticator, credentials, "argon2_concurrency")
            raise RateLimitedError(_RATE_LIMITED)
        guard_acquired = True

    try:
        ctx = authenticator.authenticate(credentials)
    except AuthenticationError:
        _record_denial(audit, authenticator, credentials, "authenticate")
        raise
    finally:
        if guard_acquired:
            argon2_guard.release()

    token = set_current(ctx)
    try:
        yield ctx
    finally:
        reset_current(token)


def _record_denial(audit, authenticator, credentials, action) -> None:
    """入口拒绝落一条审计（security.md §7.2）：``action`` 区分限流与认证失败。

    每次拒绝都记，无阈值聚合——限流器的计数器目前只用于准入判断，不对外暴露
    统计；要做「同一 peer 连续失败 N 次告警」还需要一个独立的失败计数维度
    （限流桶按请求数计，不区分成功与失败），那是可观测性设计，不在本期。

    ``actor`` 是空 ``Scope()``——身份未知，**不可用调用方声明的任何值填充**。
    ``detail`` 里不放 api_key、不放 key 前缀（§7.5 PII 脱敏），也不放桶余量
    （那能用来反推限流参数）。

    暂不记录认证失败的细分原因（``missing_credentials`` / ``unknown_principal`` /
    ``bad_gateway_key``）：三个 authenticator 都刻意只抛同一个笼统消息，要拿到
    细分原因得在 authenticator 侧另开一条只进审计的通道。那是独立设计，
    不顺手塞进本期。
    """
    if audit is None:
        return
    try:
        audit.record(
            AuditEvent(
                actor=Scope(),
                action=action,
                decision="deny",
                layer="security",
                detail={
                    "auth_mode": authenticator.mode().value,
                    "peer": credentials.peer_address,
                },
            )
        )
    except Exception:  # pragma: no cover - 审计后端故障不该把 401/429 变成 500
        pass
