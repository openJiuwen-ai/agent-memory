# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""verify_audit 的 surface 暴露边界与 PEP 契约（PR3 接口文档 §6.1）。

接口先行版：用 stub provider 验证 PEP，不实现任何密码学；真实认证接入前 generic
dispatch 不注册该管理面 verb，避免把 payload actor 当成可信根身份。
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.api.memory_api_impl.local_memory_api import LocalMemoryAPI
from jiuwen_memory.common.errors import RateLimitedError, ValidationError
from jiuwen_memory.common.security.audit_integrity.base import (
    AnchorState,
    AuditIntegrityError,
    AuditIntegrityProvider,
    AuditIntegrityStatus,
    AuditVerificationLimits,
    AuditVerificationResult,
    Proof,
)
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.security.types import Action
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import Config
from jiuwen_memory_entry.core.legacy_request_adapter import build_legacy_dispatch_request

pytestmark = pytest.mark.unit

_BOOTSTRAP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "jiuwen_memory_entry",
    "core",
)
if _BOOTSTRAP not in sys.path:
    sys.path.append(_BOOTSTRAP)

handler = importlib.import_module("handler")


def _clean_result() -> AuditVerificationResult:
    return AuditVerificationResult(
        status=AuditIntegrityStatus.CLEAN,
        checked_count=5,
        error_count=0,
        truncated=False,
        high_water_mark=5,
        key_epoch_range=(1, 1),
        anchor=AnchorState(checked=False),
    )


def _srv(api) -> object:
    class _Srv:
        def __init__(self, api_obj) -> None:
            self.api = api_obj

    return _Srv(api)


def test_generic_dispatch_does_not_expose_verify_audit_before_real_authentication() -> None:
    """payload actor 不能构造根权限；真实认证接入前管理面 verb 不注册。"""
    class _Api:
        called = False

        def verify_audit(self, **kwargs):
            self.called = True
            return _clean_result()

    api = _Api()
    status, body = handler.dispatch(
        _srv(api), build_legacy_dispatch_request("verify_audit", {"actor_user": "root"})
    )

    assert status == 404
    assert body["error"] == "UnknownVerb"
    assert api.called is False


# -- LocalMemoryAPI（PEP）契约 -------------------------------------------- #


class _StubProvider(AuditIntegrityProvider):
    def __init__(
        self,
        result: AuditVerificationResult | None = None,
        error: Exception | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self.result = result or _clean_result()
        self.error = error
        self.calls = calls
        self.verify_kwargs = None

    def chain_store(self):
        return self

    def capabilities(self):
        raise NotImplementedError

    def record_chained(self, event):
        raise NotImplementedError

    def verify(self, **kwargs) -> AuditVerificationResult:
        if self.calls is not None:
            self.calls.append("provider.verify")
        self.verify_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.result

    def active_key_ref(self):
        raise NotImplementedError

    def health(self) -> None:
        return None


class _StubGuard:
    def __init__(self, *, capacity: int = 1) -> None:
        self._capacity = capacity
        self.acquired = 0
        self.released = 0

    def acquire(self) -> bool:
        if self.acquired - self.released >= self._capacity:
            return False
        self.acquired += 1
        return True

    def release(self) -> None:
        self.released += 1


def _local_api(
    *,
    provider: AuditIntegrityProvider | None = None,
    guard: _StubGuard | None = None,
    limits: AuditVerificationLimits | None = None,
) -> tuple[LocalMemoryAPI, MagicMock, MagicMock, MagicMock]:
    permission = MagicMock()
    permission.decide.return_value = SimpleNamespace(allowed=True, rule="test", axis=None)
    audit_logger = MagicMock()
    governor = MagicMock()
    governor.audit.return_value = []
    api = LocalMemoryAPI(
        engine=MagicMock(),
        permission=permission,
        scheduler=MagicMock(),
        policy=MagicMock(),
        governor=governor,
        audit_logger=audit_logger,
        space=MagicMock(),
        ingest_jobs=MagicMock(),
        audit_integrity_provider=provider,
        audit_verify_guard=guard,
        audit_verify_limits=limits,
    )
    return api, permission, audit_logger, governor


def test_verify_audit_without_provider_returns_unsupported() -> None:
    """未装配 provider：诚实返回 unsupported，不抛错、不降级成 clean。"""
    api, permission, _, _ = _local_api()

    result = api.verify_audit(security=legacy_request_context(Scope()))

    assert permission.decide.call_args.args[2] is Action.VERIFY_AUDIT
    assert result.status is AuditIntegrityStatus.UNSUPPORTED
    assert result.checked_count == 0
    assert result.high_water_mark == 0
    assert result.detail == "audit integrity provider not configured"
    assert result.to_body()["status"] == "unsupported"


def test_verify_audit_with_provider_returns_result_and_releases_guard() -> None:
    """装配 provider：结果透传，WorkloadGuard 槽位在 finally 中归还（含异常路径外）。"""
    provider = _StubProvider()
    guard = _StubGuard()
    api, _, _, _ = _local_api(provider=provider, guard=guard)

    result = api.verify_audit(
        security=legacy_request_context(Scope()),
        after_sequence=3,
        page_size=50,
        max_samples=2,
        anchor_policy="skip",
    )

    assert result.status is AuditIntegrityStatus.CLEAN
    assert provider.verify_kwargs == {
        "after_sequence": 3,
        "page_size": 50,
        "max_samples": 2,
        "anchor_policy": "skip",
    }
    assert guard.acquired == 1
    assert guard.released == 1


def test_verify_audit_rejected_when_workload_budget_exhausted() -> None:
    """guard 耗尽即拒绝（fail-closed），不执行重计算、不占用 provider。"""
    provider = _StubProvider()
    guard = _StubGuard(capacity=0)
    api, _, audit_logger, _ = _local_api(provider=provider, guard=guard)

    with pytest.raises(RateLimitedError, match="workload budget"):
        api.verify_audit(security=legacy_request_context(Scope()))

    assert provider.verify_kwargs is None
    assert audit_logger.record.call_count == 1
    limited = audit_logger.record.call_args.args[0]
    assert limited.decision == "allow"
    assert limited.detail["permission_reason"] == "permission check passed"
    assert limited.detail["workload_guard"] == "exhausted"


def test_verify_audit_releases_guard_when_provider_raises() -> None:
    calls: list[str] = []
    provider = _StubProvider(error=AuditIntegrityError("verification failed"), calls=calls)
    guard = _StubGuard()
    api, _, audit_logger, _ = _local_api(provider=provider, guard=guard)

    def _record_call(_event) -> None:
        calls.append("audit.record")

    audit_logger.record.side_effect = _record_call
    actor = Scope(org="acme", user="auditor")

    with pytest.raises(AuditIntegrityError, match="verification failed"):
        api.verify_audit(security=legacy_request_context(actor))

    assert calls == ["audit.record", "provider.verify"]
    assert audit_logger.record.call_count == 1
    recorded = audit_logger.record.call_args.args[0]
    assert recorded.actor == actor
    assert recorded.action == "verify_audit"
    assert recorded.decision == "allow"
    assert recorded.target == Scope()
    assert recorded.occurred_at is not None
    assert guard.acquired == 1
    assert guard.released == 1


def test_verify_audit_clamps_requests_to_trusted_server_limits() -> None:
    provider = _StubProvider()
    api, _, _, _ = _local_api(
        provider=provider,
        guard=_StubGuard(),
        limits=AuditVerificationLimits(max_page_size=50, max_samples=2),
    )

    api.verify_audit(
        security=legacy_request_context(Scope()),
        page_size=51,
        max_samples=3,
    )

    assert provider.verify_kwargs == {
        "after_sequence": 0,
        "page_size": 50,
        "max_samples": 2,
        "anchor_policy": "if_configured",
    }


def test_verify_audit_truncates_provider_samples_to_effective_limit() -> None:
    samples = tuple(
        Proof(
            format_version=1,
            sequence=sequence,
            previous_digest="0" * 64,
            digest=f"{sequence:064x}",
            key_id="kp-1",
            key_epoch=1,
        )
        for sequence in range(1, 4)
    )
    provider = _StubProvider(
        AuditVerificationResult(
            status=AuditIntegrityStatus.TAMPERED,
            checked_count=3,
            error_count=3,
            truncated=False,
            high_water_mark=3,
            key_epoch_range=(1, 1),
            anchor=AnchorState(checked=False),
            samples=samples,
        )
    )
    api, _, _, _ = _local_api(provider=provider, guard=_StubGuard())

    result = api.verify_audit(
        security=legacy_request_context(Scope()),
        max_samples=2,
    )

    assert [sample.sequence for sample in result.samples] == [1, 2]
    assert result.truncated is True


def test_audit_preserves_legacy_read_action_while_verify_uses_new_action() -> None:
    api, permission, _, _ = _local_api()
    security = legacy_request_context(Scope())

    def _decide(_identity, _target, action, **_kwargs):
        return SimpleNamespace(
            allowed=action in {Action.READ, Action.VERIFY_AUDIT},
            rule="legacy-root-grant",
            axis=None,
        )

    permission.decide.side_effect = _decide

    api.audit({}, security=security)
    assert permission.decide.call_args.args[2] is Action.READ

    permission.decide.reset_mock()
    api.verify_audit(security=security)
    assert permission.decide.call_args.args[2] is Action.VERIFY_AUDIT


def test_provider_requires_dedicated_verify_guard() -> None:
    with pytest.raises(ValidationError, match="dedicated audit_verify_guard"):
        _local_api(provider=_StubProvider())


def test_interface_only_audit_integrity_config_fails_closed() -> None:
    config = Config.from_dict({"audit_integrity": {"default": {"target": "chained_hmac"}}})

    with pytest.raises(ValidationError, match="interface-only"):
        build_kernel(config=config)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"after_sequence": -1},
        {"after_sequence": True},
        {"page_size": 0},
        {"page_size": "100"},
        {"max_samples": -1},
        {"max_samples": False},
        {"anchor_policy": "always"},
    ],
)
def test_verify_audit_validates_direct_api_parameters(kwargs) -> None:
    api, _, _, _ = _local_api()

    with pytest.raises(ValidationError):
        api.verify_audit(security=legacy_request_context(Scope()), **kwargs)


def test_verify_audit_signature_is_keyword_only_server_side_params() -> None:
    """PEP 入参只有 security 与四个服务端验证参数：不接受调用方传入 digest/key/proof。"""
    params = inspect.signature(LocalMemoryAPI.verify_audit).parameters
    assert list(params) == [
        "self",
        "security",
        "after_sequence",
        "page_size",
        "max_samples",
        "anchor_policy",
    ]
    for name in ("security", "after_sequence", "page_size", "max_samples", "anchor_policy"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
