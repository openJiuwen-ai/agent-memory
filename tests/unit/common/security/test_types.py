"""common.security.types: 值对象不可变性、actor 必填、ContextVar 传播与线程隔离。"""

from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from common.errors import AgentMemoryError, AuthenticationError, PermissionDeniedError
from common.security.types import (
    ROLE_RANK,
    AuthContext,
    RequestSecurityContext,
    Role,
    Surface,
    get_current,
    reset_current,
    set_current,
)
from common.type_def.scope import Scope

pytestmark = pytest.mark.unit


def test_actor_has_no_default() -> None:
    """无参构造必须失败：否则「忘了传 actor」会静默得到 ROOT 的空 Scope()。"""
    with pytest.raises(TypeError):
        AuthContext()  # type: ignore[call-arg]


def test_context_is_frozen() -> None:
    ctx = AuthContext(actor=Scope(org="acme", user="alice"))
    with pytest.raises(FrozenInstanceError):
        ctx.actor = Scope()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        ctx.role = Role.ROOT  # type: ignore[misc]


def test_defaults_are_least_privilege() -> None:
    ctx = AuthContext(actor=Scope(org="acme", user="alice"))
    assert ctx.role is Role.USER
    assert ctx.delegation_id == ""
    assert ctx.credential_id == ""
    assert ctx.expires_at is None


def test_role_is_str_for_audit_detail() -> None:
    """Role 要能直接进 AuditEvent.detail（dict[str, str]），无需转换。"""
    detail: dict[str, str] = {"role": Role.ADMIN}
    assert detail["role"] == "admin"


def test_role_rank_is_ordered() -> None:
    assert ROLE_RANK[Role.USER] < ROLE_RANK[Role.ADMIN] < ROLE_RANK[Role.ROOT]


def test_unknown_role_rejected_at_construction() -> None:
    """拼错的角色名在构造点就炸，而不是在权限判断时静默走 else 分支。"""
    with pytest.raises(ValueError):
        Role("superuser")


# --- 过期判定 ---


def test_no_expiry_never_expires() -> None:
    """``expires_at=None`` 表示不随上下文过期，不能被读成「已过期」。"""
    assert AuthContext(actor=Scope(org="acme", user="alice")).is_expired() is False


def test_expiry_boundary_is_inclusive() -> None:
    """到点即失效：``now == expires_at`` 判过期，不留一个刚好等于的放行缝。"""
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ctx = AuthContext(actor=Scope(org="acme", user="alice"), expires_at=moment)
    assert ctx.is_expired(now=moment) is True
    assert ctx.is_expired(now=moment - timedelta(seconds=1)) is False


# --- RequestSecurityContext ---


def test_request_context_attributes_are_read_only() -> None:
    """``attributes`` 参与 PR2 的授权环境：构造后不能再被任何持有者改写。"""
    ctx = RequestSecurityContext(
        auth=AuthContext(actor=Scope(org="acme", user="alice")),
        attributes={"tenant_tier": "gold"},
    )
    with pytest.raises(TypeError):
        ctx.attributes["tenant_tier"] = "platinum"  # type: ignore[index]


def test_request_context_copies_the_source_mapping() -> None:
    """传入的 dict 事后被改，不能影响已建立的请求上下文。"""
    source = {"tenant_tier": "gold"}
    ctx = RequestSecurityContext(
        auth=AuthContext(actor=Scope(org="acme", user="alice")),
        attributes=source,
    )
    source["tenant_tier"] = "platinum"
    assert ctx.attributes["tenant_tier"] == "gold"


def test_request_context_actor_comes_from_auth() -> None:
    """actor 只能来自认证产出，不是请求上下文自带的可写字段。"""
    auth = AuthContext(actor=Scope(org="acme", user="alice"))
    ctx = RequestSecurityContext(auth=auth, surface=Surface.HTTP)
    assert ctx.actor == Scope(org="acme", user="alice")
    with pytest.raises(AttributeError):
        ctx.actor = Scope(org="evil")  # type: ignore[misc]


def test_request_context_surface_defaults_to_internal() -> None:
    """未声明接入形态时按进程内算，不猜成某个网络 surface。"""
    ctx = RequestSecurityContext(auth=AuthContext(actor=Scope()))
    assert ctx.surface is Surface.INTERNAL


# --- ContextVar 传播 ---


def test_get_current_is_none_without_authentication() -> None:
    """未认证返回 None 而非默认 AuthContext——后者是 fail-open。"""
    assert get_current() is None


def test_set_then_reset_restores_none() -> None:
    ctx = AuthContext(actor=Scope(org="acme", user="alice"))
    token = set_current(ctx)
    try:
        assert get_current() is ctx
    finally:
        reset_current(token)
    assert get_current() is None


def test_nested_set_restores_outer_context() -> None:
    outer = AuthContext(actor=Scope(org="acme", user="alice"))
    inner = AuthContext(actor=Scope(org="evil", user="mallory"))
    outer_token = set_current(outer)
    inner_token = set_current(inner)
    assert get_current() is inner
    reset_current(inner_token)
    assert get_current() is outer
    reset_current(outer_token)
    assert get_current() is None


def test_threads_do_not_share_context() -> None:
    """ThreadingHTTPServer 每请求一线程：一个线程的身份绝不能被另一个看到。"""
    seen: dict[str, AuthContext | None] = {}
    started = threading.Event()

    def worker() -> None:
        seen["before_main_set"] = get_current()
        token = set_current(AuthContext(actor=Scope(org="evil", user="mallory")))
        started.set()
        seen["own"] = get_current()
        reset_current(token)

    main_token = set_current(AuthContext(actor=Scope(org="acme", user="alice")))
    try:
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert seen["before_main_set"] is None  # 主线程的身份不泄漏进子线程
        assert seen["own"].actor == Scope(org="evil", user="mallory")
        assert get_current().actor == Scope(org="acme", user="alice")  # 未被子线程污染
    finally:
        reset_current(main_token)


# --- AuthenticationError ---


def test_authentication_error_is_distinct_from_permission_denied() -> None:
    """401「不知道你是谁」与 403「知道但不许」必须可分，否则 HTTP 层无法映射。"""
    assert issubclass(AuthenticationError, AgentMemoryError)
    assert not issubclass(AuthenticationError, PermissionDeniedError)
    assert not issubclass(PermissionDeniedError, AuthenticationError)
