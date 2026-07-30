"""审计事件记录认证元数据（security.md §7.2 / PR③-2）。

PR③ 在 ``_record_audit`` 从请求级 ``AuthContext`` 取 ``acting_user`` / ``role`` /
``key_fp`` / ``auth_mode`` 塞进 ``AuditEvent.detail``。§7.2 要求审计记录这四样
认证元数据。无认证上下文（后台 job / 直连）时不填--这几项是认证产物，不存在比
填空更诚实。
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from api.memory_api_impl import build_kernel
from common.type_def import Scope
from common.type_def.auth import AuthContext, Role, reset_current, set_current
from config import Config

pytestmark = pytest.mark.unit

_ALICE = Scope(org="acme", user="alice")


@contextmanager
def _as(ctx: AuthContext):
    token = set_current(ctx)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture()
def api():
    return build_kernel(config=Config.from_dict({"permission": {"default": "sqlite"}})).api


def test_audit_detail_records_four_auth_fields_when_authenticated(api) -> None:
    """有 AuthContext 时，审计 detail 含 acting_user/role/key_fp/auth_mode。"""
    ctx = AuthContext(
        actor=_ALICE,
        acting_user="alice",
        role=Role.USER,
        authorizing_key_fp="fp-abc123",
        auth_mode="api_key",
    )
    with _as(ctx):
        api.write("content", _ALICE, identity=_ALICE)

    events = api.audit({"action": "write"}, identity=Scope(), limit=10)
    ev = next(e for e in events if e.action == "write")
    assert ev.detail.get("acting_user") == "alice"
    assert ev.detail.get("role") == "user"
    assert ev.detail.get("key_fp") == "fp-abc123"
    assert ev.detail.get("auth_mode") == "api_key"


def test_audit_detail_omits_auth_fields_when_no_context(api) -> None:
    """无 AuthContext（后台 job / 直连）时不填这四项。"""
    # 不 set_current -> get_current() 返回 None
    api.write("content", _ALICE, identity=_ALICE)

    events = api.audit({"action": "write"}, identity=Scope(), limit=10)
    ev = next(e for e in events if e.action == "write")
    # 四项都不在 detail（不存在比填空诚实）
    for key in ("acting_user", "role", "key_fp", "auth_mode"):
        assert key not in ev.detail, f"{key} 不该出现在无认证上下文的审计里"


def test_audit_detail_records_root_role_and_dev_mode(api) -> None:
    """DEV 模式 ROOT：role=root, auth_mode=dev。"""
    ctx = AuthContext(actor=Scope(), role=Role.ROOT, auth_mode="dev")
    with _as(ctx):
        api.admin_set("rerank.enabled", "false", identity=Scope())

    events = api.audit({"action": "admin_set"}, identity=Scope(), limit=10)
    ev = next(e for e in events if e.action == "admin_set")
    assert ev.detail.get("role") == "root"
    assert ev.detail.get("auth_mode") == "dev"
