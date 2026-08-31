# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""jiuwen_memory.common.security.runtime: 聚合契约--健康检查拒绝启动、关闭不中途放弃。

接口先行版：用测试内 stub 能力验证 Runtime 的聚合语义（它本就不含任何
能力算法，只做持有、健康与生命周期）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.audit_integrity.base import AuditIntegrityProvider
from jiuwen_memory.common.security.authentication.base import Authenticator
from jiuwen_memory.common.security.authorization.base import Authorizer
from jiuwen_memory.common.security.protection.binding_policy import BindingPolicy
from jiuwen_memory.common.security.protection.rate_limit import RateLimiter
from jiuwen_memory.common.security.protection.workload_guard import WorkloadGuard
from jiuwen_memory.common.security.runtime import SecurityRuntime
from jiuwen_memory.common.security.types import Credentials

pytestmark = pytest.mark.unit


class _StubAuthenticator(Authenticator):
    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy

    def authenticate(self, credentials: Credentials):
        raise NotImplementedError

    def mode(self) -> str:
        return "stub"

    def health(self) -> None:
        if not self._healthy:
            raise RuntimeError("unhealthy")


class _StubAuthorizer(Authorizer):
    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy

    def authorize(self, *, auth, resource, environment):
        raise NotImplementedError

    def health(self) -> None:
        if not self._healthy:
            raise RuntimeError("unhealthy")


class _StubLimiter(RateLimiter):
    def __init__(self, *, healthy: bool = True, close_raises: bool = False) -> None:
        self._healthy = healthy
        self._close_raises = close_raises
        self.closed = False

    def allow(self, peer: str) -> bool:
        return True

    def health(self) -> None:
        if not self._healthy:
            raise RuntimeError("unhealthy")

    def close(self) -> None:
        self.closed = True
        if self._close_raises:
            raise RuntimeError("close failed")


class _StubGuard(WorkloadGuard):
    def __init__(self, *, healthy: bool = True, close_raises: bool = False) -> None:
        self._healthy = healthy
        self._close_raises = close_raises
        self.closed = False

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None

    @property
    def max_concurrent(self) -> int:
        return 1

    def health(self) -> None:
        if not self._healthy:
            raise RuntimeError("unhealthy")

    def close(self) -> None:
        self.closed = True
        if self._close_raises:
            raise RuntimeError("close failed")


class _StubBinding(BindingPolicy):
    def health(self) -> None:
        return None

    def check(self, hosts, *, requires_loopback: bool) -> None:
        return None


class _StubAuditIntegrity(AuditIntegrityProvider):
    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy

    def capabilities(self):
        raise NotImplementedError

    def chain_store(self):
        raise NotImplementedError

    def record_chained(self, event):
        raise NotImplementedError

    def verify(self, **kwargs):
        raise NotImplementedError

    def active_key_ref(self):
        raise NotImplementedError

    def health(self) -> None:
        if not self._healthy:
            raise RuntimeError("unhealthy")


@dataclass(frozen=True)
class _RuntimeOverrides:
    authenticator: Authenticator | None = None
    authorizer: Authorizer | None = None
    limiter: RateLimiter | None = None
    guard: WorkloadGuard | None = None
    binding: BindingPolicy | None = None
    audit_integrity: AuditIntegrityProvider | None = None


def _runtime(overrides: _RuntimeOverrides | None = None) -> SecurityRuntime:
    overrides = overrides or _RuntimeOverrides()
    return SecurityRuntime(
        authenticator=overrides.authenticator or _StubAuthenticator(),
        authorizer=overrides.authorizer or _StubAuthorizer(),
        rate_limiter=overrides.limiter or _StubLimiter(),
        workload_guard=overrides.guard or _StubGuard(),
        binding_policy=overrides.binding or _StubBinding(),
        audit_integrity_provider=overrides.audit_integrity,
    )


def test_runtime_is_frozen() -> None:
    runtime = _runtime()
    with pytest.raises(FrozenInstanceError):
        runtime.authenticator = _StubAuthenticator()  # type: ignore[misc]


def test_health_raises_on_unhealthy_capability() -> None:
    """健康检查失败必须抛异常拒绝启动，不能返回 bool 诱导调用方继续。"""
    runtime = _runtime(_RuntimeOverrides(limiter=_StubLimiter(healthy=False)))
    with pytest.raises(ValidationError, match="rate_limiter"):
        runtime.health()


def test_health_covers_authorizer() -> None:
    """授权能力同样纳入健康检查：authorizer 不健康也必须拒绝启动。"""
    runtime = _runtime(_RuntimeOverrides(authorizer=_StubAuthorizer(healthy=False)))
    with pytest.raises(ValidationError, match="authorizer"):
        runtime.health()


def test_health_passes_when_all_healthy() -> None:
    _runtime().health()


def test_cryptography_provider_is_optional() -> None:
    """未配置存储加密是合法形态：该项允许 None，其余五项必须非 None。"""
    assert _runtime().cryptography_provider is None


def test_audit_integrity_provider_is_optional() -> None:
    """未启用审计完整性期是合法形态：provider 允许 None（退化为普通审计）。"""
    assert _runtime().audit_integrity_provider is None


def test_health_covers_audit_integrity_when_configured() -> None:
    """装配了 provider 就必须纳入健康检查：写审计链的能力不健康即拒绝启动。"""
    runtime = _runtime(_RuntimeOverrides(audit_integrity=_StubAuditIntegrity(healthy=False)))
    with pytest.raises(ValidationError, match="audit_integrity"):
        runtime.health()


def test_health_passes_with_healthy_audit_integrity() -> None:
    _runtime(_RuntimeOverrides(audit_integrity=_StubAuditIntegrity())).health()


def test_close_continues_after_failure() -> None:
    """一个能力关闭失败不能中断其余能力的关闭：句柄泄漏比首个异常更难排查。

    能力顺序为 authenticator -> authorizer -> rate_limiter -> workload_guard -> binding_policy；
    让排前的 limiter 抛错、排后的 guard 正常，guard.closed 为 True 即证明继续执行。
    """
    limiter = _StubLimiter(close_raises=True)
    guard = _StubGuard()
    runtime = _runtime(_RuntimeOverrides(limiter=limiter, guard=guard))
    runtime.close()  # limiter 的异常被吞掉，方法正常返回
    assert limiter.closed is True
    assert guard.closed is True


def test_close_closes_capabilities() -> None:
    guard = _StubGuard()
    runtime = _runtime(_RuntimeOverrides(guard=guard))
    runtime.close()
    assert guard.closed is True


def test_module_defers_producer_to_impl_pr() -> None:
    """接口先行版不定义会与旧 SecurityProducer 冲突的 SecurityRuntimeProducer。"""
    import jiuwen_memory.common.security.runtime as runtime_mod

    for name in dir(runtime_mod):
        obj = getattr(runtime_mod, name)
        assert not isinstance(obj, type) or not issubclass(obj, Factory), name
