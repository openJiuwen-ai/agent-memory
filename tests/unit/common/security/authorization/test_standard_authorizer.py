"""StandardAuthorizer 的授权 truth table（F05 §Authorization 决策顺序 / §5.4 验收）。

覆盖迁移计划 §5.4 点名的每一项：owner、Grant、Delegation、role、默认拒绝的完整
truth table；跨 org、跨 space、跨主体、伪造 delegation、撤销/过期 delegation 全部拒绝；
SHARE 与管理动作默认不可委托。

Store 用内存假件而不是真 SQLite：这里测的是**判定**，不是记录怎么存。假件的
``find_active`` 刻意**不做**时效过滤，好让「Authorizer 是否自己也复核一遍时效」
这件事被真正测到——真实现按契约会过滤，那样反而测不出 Authorizer 的兜底。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from common.security.authorization.authorization_impl.standard_authorizer import (
    StandardAuthorizer,
)
from common.security.authorization.store import DelegationStore, GrantStore
from common.security.types import (
    Action,
    AuthContext,
    AuthorizationEnvironment,
    Delegation,
    DenyReason,
    Grant,
    ResourceDescriptor,
    Role,
)
from common.type_def import Scope

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

ALICE = Scope(org="acme", space="main", user="alice")
ALICE_AGENT = Scope(org="acme", space="main", user="alice", agent="assistant")
BOB = Scope(org="acme", space="main", user="bob")
OTHER_ORG = Scope(org="globex", space="main", user="alice")


# ====================================================================== #
# 内存假件
# ====================================================================== #


class FakeGrantStore(GrantStore):
    def __init__(self, grants: list[Grant] | None = None) -> None:
        self.grants = list(grants or [])

    def add(self, grant: Grant) -> None:
        self.grants.append(grant)

    def revoke(self, grant_id: str) -> None:
        self.grants = [g for g in self.grants if g.grant_id != grant_id]

    def find_active(self, *, grantee, grantor_org, action, now):
        # 刻意只按 action 与 org 过滤，不滤时效——见模块 docstring。
        return [
            g for g in self.grants if action in g.actions and g.grantor.org == grantor_org
        ]

    def health(self) -> None:
        return None


class FakeDelegationStore(DelegationStore):
    def __init__(self, delegations: list[Delegation] | None = None) -> None:
        self.by_id = {d.delegation_id: d for d in (delegations or [])}

    def add(self, delegation: Delegation) -> None:
        self.by_id[delegation.delegation_id] = delegation

    def revoke(self, delegation_id: str) -> None:
        self.by_id.pop(delegation_id, None)

    def get(self, delegation_id: str):
        return self.by_id.get(delegation_id)

    def health(self) -> None:
        return None


def _authorizer(
    *, grants: list[Grant] | None = None, delegations: list[Delegation] | None = None
) -> StandardAuthorizer:
    return StandardAuthorizer(
        grant_store=FakeGrantStore(grants),
        delegation_store=FakeDelegationStore(delegations),
    )


def _resource(
    action: Action = Action.READ,
    scope: Scope = ALICE,
    *,
    resource_type: str = "memory_unit",
    attributes: dict[str, str] | None = None,
) -> ResourceDescriptor:
    return ResourceDescriptor(
        action=action,
        resource_type=resource_type,
        scope=scope,
        attributes=attributes or {},
    )


def _env(now: datetime = NOW) -> AuthorizationEnvironment:
    return AuthorizationEnvironment(now=now)


def _auth(actor: Scope = ALICE, **overrides) -> AuthContext:
    return AuthContext(actor=actor, **overrides)


def _decide(
    authorizer: StandardAuthorizer,
    auth: AuthContext,
    resource: ResourceDescriptor,
    now: datetime = NOW,
):
    return authorizer.authorize(auth=auth, resource=resource, environment=_env(now))


# ====================================================================== #
# 第 1 步：上下文时效
# ====================================================================== #


def test_expired_context_is_denied_before_anything_else() -> None:
    """过期上下文先于一切被拒——即使 owner 规则本会放行。"""
    auth = _auth(expires_at=NOW - timedelta(seconds=1))
    decision = _decide(_authorizer(), auth, _resource())
    assert not decision.allowed
    assert decision.reason is DenyReason.EXPIRED_CONTEXT


def test_expired_root_context_is_still_denied() -> None:
    """ROOT 也不能拿过期上下文操作。"""
    auth = _auth(role=Role.ROOT, expires_at=NOW - timedelta(seconds=1))
    decision = _decide(_authorizer(), auth, _resource(Action.ADMINISTER_SYSTEM))
    assert not decision.allowed
    assert decision.reason is DenyReason.EXPIRED_CONTEXT


# ====================================================================== #
# 第 2 步：空 actor 不是特权
# ====================================================================== #


def test_empty_actor_is_denied_not_privileged() -> None:
    """空 Scope 是「没填内容的身份」，不是 platform admin（F05 §授权不变量 1）。

    这是与旧 PermissionManager 的关键行为反转：旧实现在无认证上下文时把空 actor
    当全局放行。
    """
    decision = _decide(_authorizer(), _auth(actor=Scope()), _resource())
    assert not decision.allowed
    assert decision.reason is DenyReason.CONTEXT_MISMATCH


def test_empty_actor_with_root_role_is_still_denied() -> None:
    """连 ROOT 都救不了空 actor：审计需要知道**谁**做了这件事。"""
    decision = _decide(_authorizer(), _auth(actor=Scope(), role=Role.ROOT), _resource())
    assert not decision.allowed
    assert decision.reason is DenyReason.CONTEXT_MISMATCH


# ====================================================================== #
# 第 3 步：角色闸门
# ====================================================================== #


@pytest.mark.parametrize(
    "action",
    [
        Action.MANAGE_PRINCIPAL,
        Action.MANAGE_SPACE,
        Action.MANAGE_POLICY,
        Action.READ_AUDIT,
        Action.VERIFY_AUDIT,
        Action.ADMINISTER_SYSTEM,
    ],
)
def test_user_role_cannot_do_management_actions(action: Action) -> None:
    """普通用户拿不到任何管理动作——即使目标是自己的 scope。"""
    decision = _decide(_authorizer(), _auth(), _resource(action))
    assert not decision.allowed
    assert decision.reason is DenyReason.ROLE_REQUIRED


def test_admin_can_manage_within_own_org() -> None:
    auth = _auth(role=Role.ADMIN)
    decision = _decide(_authorizer(), auth, _resource(Action.MANAGE_SPACE, BOB))
    assert decision.allowed
    # 管理面由**角色**放行，不是碰巧命中了 owner——ADMIN 管理别人的 space，目标本就
    # 不在自己 scope 内。这条断言钉住的是「管理面判定在第 3 步终结」。
    assert decision.rule == "role_gate"


def test_admin_cannot_manage_across_org() -> None:
    """ADMIN 的管辖止于本 org。"""
    auth = _auth(role=Role.ADMIN)
    decision = _decide(_authorizer(), auth, _resource(Action.MANAGE_SPACE, OTHER_ORG))
    assert not decision.allowed
    assert decision.reason is DenyReason.CROSS_ORG


def test_admin_cannot_verify_audit() -> None:
    """审计链校验要 ROOT：能校验也就能知道校验何时失败，那是 ROOT 才该有的视野。"""
    decision = _decide(_authorizer(), _auth(role=Role.ADMIN), _resource(Action.VERIFY_AUDIT))
    assert not decision.allowed
    assert decision.reason is DenyReason.ROLE_REQUIRED


def test_root_passes_management_gate() -> None:
    decision = _decide(_authorizer(), _auth(role=Role.ROOT), _resource(Action.ADMINISTER_SYSTEM))
    assert decision.allowed


def test_grant_cannot_bypass_the_role_gate() -> None:
    """一条写着管理动作的 Grant 也拿不到管理面。

    闸门排在所有放行规则之前，正是为了挡住这条路径：否则谁能写 Grant，谁就能
    自助提权到管理面。
    """
    grant = Grant(
        grant_id="g1",
        grantor=BOB,
        grantee=ALICE,
        actions=frozenset({Action.MANAGE_POLICY}),
    )
    decision = _decide(_authorizer(grants=[grant]), _auth(), _resource(Action.MANAGE_POLICY, BOB))
    assert not decision.allowed
    assert decision.reason is DenyReason.ROLE_REQUIRED


# ====================================================================== #
# ROOT 与 org 硬边界
# ====================================================================== #


def test_root_crosses_org() -> None:
    decision = _decide(_authorizer(), _auth(role=Role.ROOT), _resource(Action.READ, OTHER_ORG))
    assert decision.allowed


def test_user_cannot_cross_org_even_with_grant() -> None:
    """Grant 不跨 org 生效（F05 §Grant）。"""
    grant = Grant(
        grant_id="g1",
        grantor=OTHER_ORG,
        grantee=ALICE,
        actions=frozenset({Action.READ}),
    )
    decision = _decide(_authorizer(grants=[grant]), _auth(), _resource(Action.READ, OTHER_ORG))
    assert not decision.allowed
    assert decision.reason is DenyReason.CROSS_ORG


# ====================================================================== #
# 第 4 步：owner 覆盖
# ====================================================================== #


def test_owner_reads_own_scope() -> None:
    decision = _decide(_authorizer(), _auth(), _resource())
    assert decision.allowed
    assert decision.rule == "owner_cover"


def test_owner_covers_own_agent_branch() -> None:
    decision = _decide(_authorizer(), _auth(), _resource(scope=ALICE_AGENT))
    assert decision.allowed


def test_agent_does_not_cover_its_users_scope() -> None:
    """反向不成立：agent 身份读不到 user 的完整分支（F05 §授权不变量 2）。"""
    decision = _decide(_authorizer(), _auth(actor=ALICE_AGENT), _resource(scope=ALICE))
    assert not decision.allowed
    assert decision.reason is DenyReason.NOT_COVERED


def test_cross_user_within_org_is_denied() -> None:
    decision = _decide(_authorizer(), _auth(), _resource(scope=BOB))
    assert not decision.allowed
    assert decision.reason is DenyReason.NOT_COVERED


def test_cross_space_is_denied_without_grant() -> None:
    other_space = Scope(org="acme", space="archive", user="alice")
    decision = _decide(_authorizer(), _auth(), _resource(scope=other_space))
    assert not decision.allowed


def test_principal_path_comes_from_resource_attributes() -> None:
    """主体路径来自 descriptor（PEP 从 space policy 真源构造），不来自请求声明。"""
    bot = Scope(org="acme", space="main", agent="bot")
    target = Scope(org="acme", space="main", user="alice", agent="bot")

    denied = _decide(_authorizer(), _auth(actor=bot), _resource(scope=target))
    assert not denied.allowed

    allowed = _decide(
        _authorizer(),
        _auth(actor=bot),
        _resource(scope=target, attributes={"principal_path": "agent_user"}),
    )
    assert allowed.allowed


def test_invalid_principal_path_falls_back_to_stricter_default() -> None:
    """写坏的 principal_path 回落 ``user_agent``（覆盖面更小的那个），不放宽。"""
    bot = Scope(org="acme", space="main", agent="bot")
    target = Scope(org="acme", space="main", user="alice", agent="bot")
    decision = _decide(
        _authorizer(),
        _auth(actor=bot),
        _resource(scope=target, attributes={"principal_path": "nonsense"}),
    )
    assert not decision.allowed


# ====================================================================== #
# 第 5 步：Delegation
# ====================================================================== #


def _delegation(**overrides) -> Delegation:
    base = {
        "delegation_id": "d1",
        "delegator": ALICE,
        "delegate": ALICE_AGENT,
        "actions": frozenset({Action.READ, Action.WRITE}),
        "expires_at": NOW + timedelta(hours=1),
    }
    base.update(overrides)
    return Delegation(**base)  # type: ignore[arg-type]


def test_delegation_allows_agent_to_act_for_user() -> None:
    auth = _auth(actor=ALICE_AGENT, delegation_id="d1")
    decision = _decide(_authorizer(delegations=[_delegation()]), auth, _resource(scope=ALICE))
    assert decision.allowed
    assert decision.rule == "delegation"


def test_user_to_user_delegation_is_denied() -> None:
    """F05 只允许 user 委托 agent/service；user -> user 是第二套 Grant，必须拒（P1-5）。"""
    delegation = _delegation(delegate=BOB)
    auth = _auth(actor=BOB, delegation_id="d1")
    decision = _decide(_authorizer(delegations=[delegation]), auth, _resource(scope=ALICE))
    assert not decision.allowed
    assert decision.reason is DenyReason.DELEGATION_INVALID


def test_forged_delegation_id_is_denied() -> None:
    """``AuthContext`` 里编一个 id 不管用——内容一律回真源读。"""
    auth = _auth(actor=ALICE_AGENT, delegation_id="forged")
    decision = _decide(_authorizer(delegations=[_delegation()]), auth, _resource(scope=ALICE))
    assert not decision.allowed
    assert decision.reason is DenyReason.DELEGATION_INVALID


def test_expired_delegation_is_denied() -> None:
    delegation = _delegation(expires_at=NOW - timedelta(seconds=1))
    auth = _auth(actor=ALICE_AGENT, delegation_id="d1")
    decision = _decide(_authorizer(delegations=[delegation]), auth, _resource(scope=ALICE))
    assert not decision.allowed
    assert decision.reason is DenyReason.DELEGATION_INVALID


def test_revoked_delegation_is_denied() -> None:
    delegation = _delegation(revoked=True)
    auth = _auth(actor=ALICE_AGENT, delegation_id="d1")
    decision = _decide(_authorizer(delegations=[delegation]), auth, _resource(scope=ALICE))
    assert not decision.allowed
    assert decision.reason is DenyReason.DELEGATION_INVALID


def test_delegation_cannot_be_used_by_another_agent() -> None:
    """别人的委托 id 捡去用不管用。"""
    other_agent = Scope(org="acme", space="main", user="alice", agent="rogue")
    auth = _auth(actor=other_agent, delegation_id="d1")
    decision = _decide(_authorizer(delegations=[_delegation()]), auth, _resource(scope=ALICE))
    assert not decision.allowed
    assert decision.reason is DenyReason.DELEGATION_INVALID


def test_delegation_does_not_reach_beyond_delegator_scope() -> None:
    """委托授不出委托方自己都没有的范围。"""
    auth = _auth(actor=ALICE_AGENT, delegation_id="d1")
    decision = _decide(_authorizer(delegations=[_delegation()]), auth, _resource(scope=BOB))
    assert not decision.allowed


@pytest.mark.parametrize("action", [Action.SHARE, Action.REVOKE_SHARE])
def test_share_is_never_delegatable(action: Action) -> None:
    """让被委托方能再授权，等于让一次性委托升级成永久 Grant。"""
    delegation = _delegation(actions=frozenset({Action.READ, action}))
    auth = _auth(actor=ALICE_AGENT, delegation_id="d1")
    decision = _decide(_authorizer(delegations=[delegation]), auth, _resource(action, ALICE))
    assert not decision.allowed
    assert decision.reason is DenyReason.DELEGATION_ACTION


def test_action_outside_delegation_allowlist_is_denied() -> None:
    auth = _auth(actor=ALICE_AGENT, delegation_id="d1")
    decision = _decide(
        _authorizer(delegations=[_delegation()]), auth, _resource(Action.DELETE, ALICE)
    )
    assert not decision.allowed
    assert decision.reason is DenyReason.DELEGATION_ACTION


def test_delegation_respects_allowed_spaces() -> None:
    delegation = _delegation(allowed_spaces=frozenset({"archive"}))
    auth = _auth(actor=ALICE_AGENT, delegation_id="d1")
    decision = _decide(_authorizer(delegations=[delegation]), auth, _resource(scope=ALICE))
    assert not decision.allowed
    assert decision.reason is DenyReason.DELEGATION_INVALID


def test_delegation_bound_to_credential_rejects_other_credential() -> None:
    """绑定凭据后换一把 key 就用不了——泄露的爆炸半径收敛在单把 key 上。"""
    delegation = _delegation(bound_credential_id="cred-1")
    auth = _auth(actor=ALICE_AGENT, delegation_id="d1", credential_id="cred-2")
    decision = _decide(_authorizer(delegations=[delegation]), auth, _resource(scope=ALICE))
    assert not decision.allowed
    assert decision.reason is DenyReason.DELEGATION_INVALID


def test_delegation_bound_to_credential_accepts_matching_credential() -> None:
    delegation = _delegation(bound_credential_id="cred-1")
    auth = _auth(actor=ALICE_AGENT, delegation_id="d1", credential_id="cred-1")
    decision = _decide(_authorizer(delegations=[delegation]), auth, _resource(scope=ALICE))
    assert decision.allowed


def test_failed_delegation_does_not_fall_back_to_grant() -> None:
    """声明了代操作就按代操作判：失效委托不该被一条 Grant 悄悄兜住。

    否则审计里看不出委托失效过——运维会以为代理链路一切正常。
    """
    grant = Grant(
        grant_id="g1", grantor=ALICE, grantee=ALICE_AGENT, actions=frozenset({Action.READ})
    )
    auth = _auth(actor=ALICE_AGENT, delegation_id="revoked-one")
    decision = _decide(
        _authorizer(grants=[grant], delegations=[_delegation()]), auth, _resource(scope=ALICE)
    )
    assert not decision.allowed
    assert decision.reason is DenyReason.DELEGATION_INVALID


# ====================================================================== #
# 第 6 步：Grant
# ====================================================================== #


def test_grant_allows_cross_user_access() -> None:
    grant = Grant(
        grant_id="g1", grantor=BOB, grantee=ALICE, actions=frozenset({Action.READ})
    )
    decision = _decide(_authorizer(grants=[grant]), _auth(), _resource(scope=BOB))
    assert decision.allowed
    assert decision.rule == "grant"


def test_grant_for_another_action_does_not_apply() -> None:
    grant = Grant(
        grant_id="g1", grantor=BOB, grantee=ALICE, actions=frozenset({Action.READ})
    )
    decision = _decide(_authorizer(grants=[grant]), _auth(), _resource(Action.DELETE, BOB))
    assert not decision.allowed
    assert decision.reason is DenyReason.NOT_COVERED


def test_grant_for_another_grantee_does_not_apply() -> None:
    grant = Grant(
        grant_id="g1", grantor=BOB, grantee=Scope(org="acme", space="main", user="carol"),
        actions=frozenset({Action.READ}),
    )
    decision = _decide(_authorizer(grants=[grant]), _auth(), _resource(scope=BOB))
    assert not decision.allowed


def test_grantor_must_cover_the_target() -> None:
    """授权方管不着的资源，授出去也不作数。"""
    carol = Scope(org="acme", space="main", user="carol")
    grant = Grant(
        grant_id="g1", grantor=BOB, grantee=ALICE, actions=frozenset({Action.READ})
    )
    decision = _decide(_authorizer(grants=[grant]), _auth(), _resource(scope=carol))
    assert not decision.allowed


def test_expired_grant_is_rechecked_by_authorizer() -> None:
    """Store 契约要求滤掉过期记录，Authorizer 仍复核一遍。

    时效判定必须用本次判定的同一个 ``now``；Store 用的是入参 now 还是自己取的，
    跨实现无法保证。这里的假件刻意不滤，测的就是这道兜底。
    """
    grant = Grant(
        grant_id="g1",
        grantor=BOB,
        grantee=ALICE,
        actions=frozenset({Action.READ}),
        expires_at=NOW - timedelta(seconds=1),
    )
    decision = _decide(_authorizer(grants=[grant]), _auth(), _resource(scope=BOB))
    assert not decision.allowed


def test_revoked_grant_is_rechecked_by_authorizer() -> None:
    grant = Grant(
        grant_id="g1",
        grantor=BOB,
        grantee=ALICE,
        actions=frozenset({Action.READ}),
        revoked=True,
    )
    decision = _decide(_authorizer(grants=[grant]), _auth(), _resource(scope=BOB))
    assert not decision.allowed


def test_grant_across_space_within_org_is_allowed_when_explicit() -> None:
    """跨 space 需要显式 Grant——owner 规则挡住的，Grant 可以放行。"""
    archive = Scope(org="acme", space="archive", user="alice")
    grant = Grant(
        grant_id="g1", grantor=archive, grantee=ALICE, actions=frozenset({Action.READ})
    )
    decision = _decide(_authorizer(grants=[grant]), _auth(), _resource(scope=archive))
    assert decision.allowed


# ====================================================================== #
# 第 7 步：默认拒绝
# ====================================================================== #


def test_default_deny_with_no_rules() -> None:
    decision = _decide(_authorizer(), _auth(), _resource(scope=BOB))
    assert not decision.allowed
    assert decision.reason is DenyReason.NOT_COVERED
    assert decision.rule == "default_deny"


# ====================================================================== #
# 契约形态
# ====================================================================== #


def test_authorize_arguments_are_keyword_only() -> None:
    """三个入参类型不同但都是「一坨上下文」，位置传参写反了不会报错。"""
    with pytest.raises(TypeError):
        _authorizer().authorize(_auth(), _resource(), _env())  # type: ignore[misc]


def test_authorizer_does_not_read_contextvar() -> None:
    """F05 §授权不变量 7：全部判定依据显式入参。

    ContextVar 里放一个 ROOT，判定仍按入参的 USER 走。
    """
    from common.security.types import reset_current, set_current

    token = set_current(AuthContext(actor=BOB, role=Role.ROOT))
    try:
        decision = _decide(_authorizer(), _auth(), _resource(scope=BOB))
    finally:
        reset_current(token)
    assert not decision.allowed


def test_standard_authorizer_is_not_test_only() -> None:
    assert not _authorizer().is_test_only()
