# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""新上游 dreaming 在 PR2 下的可信身份、持续复核和恢复安全边界。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from jiuwen_memory.api import Credentials, Scope, Surface, new_request_context
from jiuwen_memory.api.memory_api_impl import pep_ops
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
from jiuwen_memory.api.memory_api_impl.dreaming_security import (
    _registration_identity,
    _restored_security,
)
from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.security.authentication.key_store import fingerprint
from jiuwen_memory.common.security.types import (
    Action,
    AuthContext,
    Delegation,
    RequestSecurityContext,
    Role,
)
from jiuwen_memory.config import Config
from jiuwen_memory.construction import EvolveMode

pytestmark = pytest.mark.unit
_SCOPE = Scope(org="acme", user="alice")


class _ManualScheduler:
    """持有真实 Driver，测试显式执行 tick，避免定时线程干扰撤销顺序。"""

    supports_recurring = True

    def __init__(self):
        self.jobs = []
        self.cancelled = []

    @staticmethod
    def validate(job):
        assert job.interval > 0

    async def submit(self, job, channel):
        del channel
        self.jobs.append(job)
        return f"timer-{len(self.jobs)}"

    def cancel(self, job_id):
        self.cancelled.append(job_id)


@pytest.fixture(name="secured_dreaming")
def _secured_dreaming(monkeypatch):
    # 将真实 PEP 与凭据真源接到手动调度器，私有访问仅用于白盒回归。
    # pylint: disable=protected-access
    kernel = _build_kernel(
        config=Config.from_dict(
            {
                "security": {
                    "default": {
                        "target": "standard",
                        "params": {
                            "authenticator": {
                                "target": "api_key",
                                "params": {"root_api_key": "test-root"},
                            },
                        },
                    }
                },
            }
        )
    )
    auth = kernel.security_runtime.authenticator
    token = auth.key_store.issue(_SCOPE, Role.USER)
    security = new_request_context(
        auth.authenticate(Credentials(api_key=token)), surface=Surface.SDK
    )
    scheduler = _ManualScheduler()
    monkeypatch.setattr(kernel.api._dreaming, "_scheduler", scheduler)
    execute = AsyncMock(return_value="child-job")
    monkeypatch.setattr(kernel.api._commands, "evolve", execute)
    try:
        yield kernel, auth, token, security, scheduler, execute
    finally:
        kernel.api.release_dreaming_lock()
        kernel.ingest_jobs.close()
        kernel.scheduler.shutdown()
        kernel.security_runtime.close()


@pytest.mark.parametrize("phase", ["tick", "restore"])
@pytest.mark.parametrize("failure", ["revoked", "expired"])
def test_tick_and_restore_recheck_original_credentials(
    secured_dreaming, monkeypatch, phase, failure
):
    # 在同源凭据 Store 撤销或推进 PEP 时钟，拒绝必须先于任何业务执行。
    # pylint: disable=protected-access
    kernel, auth, token, security, scheduler, execute = secured_dreaming
    expires = datetime.now(UTC) + timedelta(minutes=1)
    security = new_request_context(replace(security.auth, expires_at=expires), surface=Surface.SDK)
    kernel.api.evolve(_SCOPE, EvolveMode.EXTRACT, dreaming=True, interval=60, security=security)
    if failure == "revoked":
        auth.key_store.revoke(fingerprint(token))
    else:

        class _AfterExpiry(datetime):
            @classmethod
            def now(cls, tz=None):
                del tz
                return expires + timedelta(seconds=1)

        monkeypatch.setattr(pep_ops, "datetime", _AfterExpiry)
    if phase == "tick":
        with pytest.raises(PermissionDeniedError):
            asyncio.run(scheduler.jobs[0].run())
    else:
        assert kernel.api.restore_dreaming() == []
    execute.assert_not_called()
    assert kernel.api._dreaming._registry.find(_SCOPE, "extract") is None


def test_restore_preserves_role_and_credential_binding(secured_dreaming):
    # 服务端注册表保留原认证事实，重启来源证明重建不能丢失角色或凭据能力。
    # pylint: disable=protected-access
    kernel, auth, token, security, scheduler, execute = secured_dreaming
    root = new_request_context(
        auth.authenticate(Credentials(api_key="test-root")), surface=Surface.SDK
    )
    kernel.api.evolve(_SCOPE, EvolveMode.EXTRACT, dreaming=True, interval=60, security=root)
    saved = kernel.api._dreaming._registry.find(_SCOPE, "extract")
    restored = _restored_security(saved.created_by)
    assert restored.auth == root.auth
    assert restored.has_valid_origin()
    assert kernel.api.restore_dreaming() == ["timer-2"]
    asyncio.run(scheduler.jobs[-1].run())
    execute.assert_awaited_once()
    identity = _registration_identity(security)
    assert token not in json.dumps(identity), "注册表不得保存原始凭据"
    decoded = _restored_security(identity)
    assert decoded.auth.credential_id == fingerprint(token)
    assert decoded.auth.credential_status_required is True


@pytest.mark.parametrize("tampered", [False, True])
def test_registration_rejects_untrusted_context(secured_dreaming, tampered):
    # 无来源或认证字段被替换的请求不能创建后台任务或持久化身份。
    # pylint: disable=protected-access
    kernel, _auth, _token, security, scheduler, execute = secured_dreaming
    if tampered:
        security = replace(security, auth=replace(security.auth, role=Role.ROOT))
    else:
        security = RequestSecurityContext(auth=AuthContext(actor=_SCOPE))
    with pytest.raises(PermissionDeniedError):
        kernel.api.evolve(_SCOPE, EvolveMode.EXTRACT, dreaming=True, interval=60, security=security)
    assert scheduler.jobs == []
    assert kernel.api._dreaming._registry.load_all() == []
    execute.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("credential_status_required", "false"),
        ("role", "unknown"),
        ("expires_at", "2026-10-13T00:00:00"),
    ],
)
def test_restore_rejects_malformed_security_snapshot(secured_dreaming, field, value):
    _kernel, _auth, _token, security, _scheduler, _execute = secured_dreaming
    identity = _registration_identity(security)
    key = "_request_context_v1"
    data = json.loads(identity.get(key))
    data.get("auth")[field] = value
    identity[key] = json.dumps(data)
    with pytest.raises(ValueError):
        _restored_security(identity)


def test_legacy_actor_only_record_cannot_gain_security_context():
    assert _restored_security({"org": "acme", "user": "alice"}) is None


@pytest.mark.parametrize("phase", ["tick", "restore"])
def test_background_rechecks_delegation_store(secured_dreaming, phase):
    # 原委托来自 PDP 的唯一 Store，持久化的委托 ID 不构成永久授权。
    # pylint: disable=protected-access
    kernel, auth, _token, _security, scheduler, execute = secured_dreaming
    actor = Scope(org="acme", agent="cron")
    token = auth.key_store.issue(actor, Role.USER)
    security = new_request_context(
        replace(auth.authenticate(Credentials(api_key=token)), delegation_id="background"),
        surface=Surface.SDK,
    )
    store = kernel.api._authorizer._delegations
    store.add(
        Delegation(
            delegation_id="background",
            delegator=_SCOPE,
            delegate=actor,
            actions=frozenset({Action.WRITE}),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            bound_credential_id=fingerprint(token),
        )
    )
    kernel.api.evolve(_SCOPE, EvolveMode.EXTRACT, dreaming=True, interval=60, security=security)
    store.revoke("background")
    if phase == "tick":
        with pytest.raises(PermissionDeniedError):
            asyncio.run(scheduler.jobs[0].run())
    else:
        assert kernel.api.restore_dreaming() == []
    execute.assert_not_called()
    assert kernel.api._dreaming._registry.find(_SCOPE, "extract") is None
