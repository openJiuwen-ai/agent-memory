# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""请求作用域的安全上下文——凭据提取 + ``RequestSecurityContext`` 构造。

各 surface（HTTP / MCP / CLI 直连）用同一条中间件：把本形态的凭据材料归一成
:class:`~common.security.types.Credentials`，交给装配好的 ``Authenticator``，把产出的
``AuthContext`` 包成 :class:`~common.security.types.RequestSecurityContext`` yield 给
调用方——它是 ``MemoryAPI`` 公开方法的唯一显式安全输入。

**本模块不决定认证策略**——模式（dev / trusted / api_key）由配置在装配期选定，
这里只负责「在正确的时机调用它、并保证退出时清理干净」。

上下文经**参数**下传，不经 ContextVar：ContextVar 在本模块仍会设置，但只作
日志/trace 的辅助传播，授权判定不得依赖它存在。
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)
if _SRC not in sys.path:
    sys.path.append(_SRC)

# ruff: noqa: E402
from jiuwen_memory.api import (
    AuditEvent,
    AuthenticationError,
    Credentials,
    RateLimitedError,
    RequestSecurityContext,
    Scope,
    Surface,
    get_request_id,
    new_request_context,
    reset_current,
    reset_request_id,
    set_current,
    set_request_id,
)

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
    bearer_len = len(_BEARER)
    if auth[:bearer_len].lower() == _BEARER:
        api_key = auth[bearer_len:].strip()
    if not api_key:
        api_key = normalized.get("x-api-key", "").strip()

    return Credentials(api_key=api_key, headers=normalized, peer_address=peer_address)


@contextmanager
def authenticated(
    authenticator,
    credentials,
    audit=None,
    limiter=None,
    *,
    workload_guard=None,
    surface=None,
    request_id: str | None = None,
) -> Iterator[RequestSecurityContext]:
    """在请求作用域内建立可信 :class:`RequestSecurityContext`；退出时**必定** reset。

    产出的上下文由调用方**显式**传给 ``MemoryAPI`` 公开方法--它是唯一的
    安全输入。ContextVar 仍在这里设置，但只供日志/trace 关联，授权不读它。

    reset 放 ``finally`` 是硬性要求：``ThreadingHTTPServer`` 每请求一线程，
    但线程可能被池化复用；漏 reset 会让下一个请求继承上一个请求的身份——
    最严重的一类越权。

    ``request_id`` 仅允许受控适配层传入。HTTP 入口生成后传给这里，使认证失败、
    限流拒绝也能关联响应和拒绝审计；缺省时由 ``new_request_context`` 生成 ID。

    ``limiter`` 在 ``authenticate`` **之前**执行（F05 §请求执行流程）：认证本身就是
    要保护的资源——API_KEY 模式下每次 authenticate 跑一次 Argon2id verify（128 MiB ×
    time_cost=4），放在认证之后限流就等于「先让攻击者把 CPU 用掉，再告诉他
    超限了」。``limiter=None`` 表示不限流（进程内直连 / MCP stdio 无网络对端）。

    ``workload_guard`` 是昂贵操作的全局并发预算（F05 §Protection §WorkloadGuard）：
    IP 桶限请求速率，限不住「同时在跑的 Argon2 verify 数」。耗尽即快速拒绝（429）
    而不是排队——无界排队只是把资源耗尽从 CPU/内存转移到线程和请求队列。在 limiter
    之后、authenticate 之前执行；acquire 成功后用 ``finally`` 释放。``None`` 表示
    该认证实现声明不需要预算保护（见 ``Authenticator.requires_concurrency_guard``）。

    ``surface`` 由适配层写入（迁移计划 §5.2 第 7 项），调用方不能经 payload 声明；
    缺省 ``INTERNAL`` 对应进程内装配。
    """
    request_id_token = set_request_id(request_id) if request_id else None
    try:
        if limiter is not None and not limiter.allow(credentials.peer_address):
            _record_denial(audit, authenticator, credentials, "rate_limit")
            raise RateLimitedError(_RATE_LIMITED)

        guard_acquired = False
        if workload_guard is not None:
            if not workload_guard.acquire():
                _record_denial(audit, authenticator, credentials, "workload_budget")
                raise RateLimitedError(_RATE_LIMITED)
            guard_acquired = True

        try:
            ctx = authenticator.authenticate(credentials)
        except AuthenticationError:
            _record_denial(audit, authenticator, credentials, "authenticate")
            raise
        finally:
            if guard_acquired and workload_guard is not None:
                workload_guard.release()

        security = new_request_context(
            ctx,
            surface=surface if surface is not None else Surface.INTERNAL,
            peer=_normalized_peer(credentials),
            request_id=request_id,
            # attributes 留空：业务 payload 不得注入任何系统属性。
            # 可信代理链、mTLS 主体等属性将来只能由服务端组件写入。
        )

        token = set_current(ctx)
        if request_id_token is None:
            request_id_token = set_request_id(security.request_id)
        try:
            yield security
        finally:
            reset_current(token)
    finally:
        if request_id_token is not None:
            reset_request_id(request_id_token)


def _normalized_peer(credentials) -> str:
    """规范化连接来源：只采信传输层对端地址。

    刻意**不读** ``X-Forwarded-For`` / ``X-Real-IP``：没有可信代理白名单时采信这类
    header，等于让调用方自述来源--限流分桶、审计溯源和将来基于 peer 的策略会同时
    被绕过。要支持反向代理部署，得先有「哪些前置跳是可信的」这项配置，那是独立设计。
    """
    return str(credentials.peer_address or "").strip()


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
        mode = authenticator.mode()
        detail = {
            "mode": str(getattr(mode, "value", mode)),
            "peer": credentials.peer_address,
        }
        request_id = get_request_id()
        if request_id:
            detail["request_id"] = request_id
        audit.record(
            AuditEvent(
                actor=Scope(),
                action=action,
                decision="deny",
                layer="security",
                detail=detail,
            )
        )
    except Exception:  # pragma: no cover - 审计后端故障不该把 401/429 变成 500
        pass
