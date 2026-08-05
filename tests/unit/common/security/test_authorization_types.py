"""授权类型的安全约束（F05 §Action / §ResourceDescriptor / §Grant / §Delegation）。

这些断言钉住的是**默认拒绝**与**不可伪造**两件事：动作集合封闭、管理动作不可委托、
时效与撤销真的生效、请求写不进只读的安全属性。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from common.security.types import (
    DELEGATABLE_ACTIONS,
    MANAGEMENT_ACTIONS,
    Action,
    AuthorizationEnvironment,
    AuthContext,
    Delegation,
    DenyReason,
    Grant,
    RequestSecurityContext,
    ResourceDescriptor,
    Role,
    Surface,
)
from common.type_def import Scope

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


# ====================================================================== #
# Action
# ====================================================================== #


def test_management_actions_are_never_delegatable() -> None:
    """管理动作默认不可委托（F05 §授权不变量 4）。"""
    assert not (MANAGEMENT_ACTIONS & DELEGATABLE_ACTIONS)


def test_share_actions_are_not_delegatable() -> None:
    """被委托方不能再授权——否则委托关系自我复制，撤销追不上。"""
    assert Action.SHARE not in DELEGATABLE_ACTIONS
    assert Action.REVOKE_SHARE not in DELEGATABLE_ACTIONS


def test_delegatable_is_an_allowlist_not_a_complement() -> None:
    """可委托集合是白名单：新增 Action 不会自动获得可委托性。

    这条测试的价值在于它会随 Action 扩张而**主动变红**：加了新动作却没想清楚
    它该不该可委托时，这里会提醒。取反写法（非管理即可委托）则会静默放行。
    """
    covered = DELEGATABLE_ACTIONS | MANAGEMENT_ACTIONS | {Action.SHARE, Action.REVOKE_SHARE}
    uncategorised = set(Action) - covered
    assert not uncategorised, f"新增动作未归类，默认应拒绝：{uncategorised}"


# ====================================================================== #
# Grant
# ====================================================================== #


def test_grant_expired_is_inactive() -> None:
    grant = Grant(
        grant_id="g1",
        grantor=Scope(org="acme", user="alice"),
        grantee=Scope(org="acme", user="bob"),
        actions=frozenset({Action.READ}),
        expires_at=NOW - timedelta(seconds=1),
    )
    assert not grant.is_active(now=NOW)


def test_grant_revoked_is_inactive_even_when_unexpired() -> None:
    """撤销优先于有效期：撤销后立即失效，不等过期。"""
    grant = Grant(
        grant_id="g1",
        grantor=Scope(org="acme", user="alice"),
        grantee=Scope(org="acme", user="bob"),
        actions=frozenset({Action.READ}),
        expires_at=NOW + timedelta(days=365),
        revoked=True,
    )
    assert not grant.is_active(now=NOW)


def test_grant_without_expiry_stays_active() -> None:
    grant = Grant(
        grant_id="g1",
        grantor=Scope(org="acme", user="alice"),
        grantee=Scope(org="acme", user="bob"),
        actions=frozenset({Action.READ}),
    )
    assert grant.is_active(now=NOW)


# ====================================================================== #
# Delegation
# ====================================================================== #


def _delegation(**overrides: object) -> Delegation:
    base: dict[str, object] = {
        "delegation_id": "d1",
        "delegator": Scope(org="acme", user="alice"),
        "delegate": Scope(org="acme", user="alice", agent="assistant"),
        "actions": frozenset({Action.READ, Action.WRITE}),
        "expires_at": NOW + timedelta(hours=1),
    }
    base.update(overrides)
    return Delegation(**base)  # type: ignore[arg-type]


def test_delegation_requires_explicit_expiry() -> None:
    """``expires_at`` 无默认值：代操作授权必须有限期。"""
    with pytest.raises(TypeError):
        Delegation(  # type: ignore[call-arg]
            delegation_id="d1",
            delegator=Scope(org="acme", user="alice"),
            delegate=Scope(org="acme", user="alice", agent="assistant"),
            actions=frozenset({Action.READ}),
        )


def test_delegation_expired_is_inactive() -> None:
    assert not _delegation(expires_at=NOW - timedelta(seconds=1)).is_active(now=NOW)


def test_delegation_revoked_is_inactive() -> None:
    assert not _delegation(revoked=True).is_active(now=NOW)


def test_delegation_not_yet_valid_is_inactive() -> None:
    assert not _delegation(not_before=NOW + timedelta(minutes=5)).is_active(now=NOW)


def test_delegation_permits_only_allowlisted_actions() -> None:
    delegation = _delegation()
    assert delegation.permits(Action.READ)
    assert not delegation.permits(Action.DELETE)  # 不在本条 allowlist 内


def test_delegation_cannot_permit_management_even_if_recorded() -> None:
    """一条写坏或被篡改的委托记录也拿不到管理动作。

    ``permits`` 同时查数据（allowlist）与策略（DELEGATABLE_ACTIONS），后者是
    存储层污染兜不住的那道。
    """
    tampered = _delegation(actions=frozenset({Action.READ, Action.ADMINISTER_SYSTEM}))
    assert not tampered.permits(Action.ADMINISTER_SYSTEM)
    assert tampered.permits(Action.READ)


# ====================================================================== #
# ResourceDescriptor / AuthorizationEnvironment
# ====================================================================== #


def test_resource_descriptor_attributes_are_read_only() -> None:
    """安全属性冻结成只读映射：拿到引用也改不动判定依据。"""
    descriptor = ResourceDescriptor(
        action=Action.READ,
        resource_type="memory_unit",
        scope=Scope(org="acme", user="alice"),
        attributes={"memory_type": "episodic"},
    )
    with pytest.raises(TypeError):
        descriptor.attributes["memory_type"] = "coding"  # type: ignore[index]


def test_resource_descriptor_copies_attributes_at_construction() -> None:
    """构造时复制入参：构造方留着的引用改不动已定型的判定依据。

    比「只读映射挡住直接赋值」更贴近真实形态——PEP 通常是从一个可变 dict 攒出
    descriptor 的，若只是包一层视图，那个 dict 在授权判定期间仍可被改。
    """
    mutable = {"memory_type": "episodic"}
    descriptor = ResourceDescriptor(
        action=Action.READ,
        resource_type="memory_unit",
        scope=Scope(org="acme", user="alice"),
        attributes=mutable,
    )
    mutable["memory_type"] = "tampered"
    assert descriptor.attributes["memory_type"] == "episodic"


def test_resource_descriptor_requires_action_and_source_of_truth_scope() -> None:
    """``action`` 与 ``scope`` 无默认值：PEP 必须显式给出动作与资源真实归属。"""
    with pytest.raises(TypeError):
        ResourceDescriptor(resource_type="memory_unit")  # type: ignore[call-arg]


def test_environment_from_request_drops_identity() -> None:
    """环境只带服务端属性，不含身份——身份是 Authorizer 的独立入参。"""
    security = RequestSecurityContext(
        auth=AuthContext(actor=Scope(org="acme", user="alice"), role=Role.USER),
        request_id="req-1",
        peer="10.0.0.1",
        surface=Surface.HTTP,
        attributes={"tls": "1.3"},
    )
    env = AuthorizationEnvironment.from_request(security, now=NOW)

    assert env.now == NOW
    assert env.surface is Surface.HTTP
    assert env.request_id == "req-1"
    assert env.peer == "10.0.0.1"
    assert env.attributes["tls"] == "1.3"
    assert not hasattr(env, "auth")
    assert not hasattr(env, "actor")


def test_environment_attributes_are_read_only() -> None:
    env = AuthorizationEnvironment(now=NOW, attributes={"tls": "1.3"})
    with pytest.raises(TypeError):
        env.attributes["tls"] = "1.2"  # type: ignore[index]


# ====================================================================== #
# DenyReason
# ====================================================================== #


def test_deny_reasons_do_not_distinguish_missing_from_forbidden() -> None:
    """不区分「资源不存在」与「无权访问」——那是资源枚举侧信道。"""
    values = {r.value for r in DenyReason}
    assert "not_found" not in values
    assert DenyReason.NOT_COVERED.value == "not_covered"
