"""判据主体的单测（F07 判定链第 6、7、10 步）。

覆盖两块：空间内的权限分档（内容轴与治理轴各档位的准入边界），以及个体记忆的隔离。
判定宿主不在本文件覆盖范围内——本模块是纯函数，宿主适配另测。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.security.space_decision import (
    DecisionOutcome,
    DenyReason,
    decide,
    on_behalf_paths_apply,
)
from jiuwen_memory.common.security.space_roles import (
    SpaceAction,
    SpaceAuthorizationFacts,
    SpaceAxis,
    SpaceContentRole,
    SpaceGovernanceRole,
    SpaceMemberFact,
)
from jiuwen_memory.common.type_def import Scope

pytestmark = pytest.mark.unit

ORG = "acme"
SPACE = "u-alice"
TARGET = Scope(org=ORG, space=SPACE)
ALICE = Scope(org=ORG, user="alice")
ALICE_VIA_A1 = Scope(org=ORG, user="alice", agent="a1")
ALICE_VIA_A2 = Scope(org=ORG, user="alice", agent="a2")
AGENT_A1 = Scope(org=ORG, agent="a1")
BOB = Scope(org=ORG, user="bob")


def _member(
    scope: Scope,
    content: SpaceContentRole = SpaceContentRole.NONE,
    governance: SpaceGovernanceRole = SpaceGovernanceRole.NONE,
) -> SpaceMemberFact:
    return SpaceMemberFact(scope=scope, content_role=content, governance_role=governance)


def _individual(owner: Scope = ALICE) -> SpaceAuthorizationFacts:
    """预建的用户主空间：有归属登记、成员表为空。"""
    return SpaceAuthorizationFacts(owners=(Scope(org=ORG, space=SPACE, user=owner.user),))


def _shared(*members: SpaceMemberFact) -> SpaceAuthorizationFacts:
    """协作空间：无归属登记、成员表非空。"""
    return SpaceAuthorizationFacts(owners=(), members=members)


def _decide(actor: Scope, **kwargs):
    params = {
        "actor": actor,
        "target": TARGET,
        "facts": _individual(),
        "entry": "search",
        "action": SpaceAction.READ,
        "axis": SpaceAxis.CONTENT,
    }
    params.update(kwargs)
    return decide(**params)


# -- 第 1 组：空间内的权限分档 --------------------------------------------- #


def test_1_1_governance_manager_cannot_read_content() -> None:
    """能管成员、看不到内容：治理轴 manager + 内容轴 none。"""
    facts = _shared(_member(BOB, governance=SpaceGovernanceRole.MANAGER))
    # 治理动作通过
    assert _decide(
        BOB,
        facts=facts,
        entry="add_space_member",
        action=SpaceAction.SHARE,
        axis=SpaceAxis.GOVERNANCE,
    ).allowed
    # 内容读取不通过
    assert not _decide(BOB, facts=facts).allowed


def test_1_2_content_editor_cannot_manage_members() -> None:
    """能读写内容、管不了成员：内容轴 editor + 治理轴 none。"""
    facts = _shared(_member(BOB, content=SpaceContentRole.EDITOR))
    assert _decide(BOB, facts=facts, action=SpaceAction.WRITE).allowed
    assert not _decide(
        BOB,
        facts=facts,
        entry="add_space_member",
        action=SpaceAction.SHARE,
        axis=SpaceAxis.GOVERNANCE,
    ).allowed


def test_1_3_contributor_may_only_change_its_own_entries() -> None:
    """只能改自己写的：contributor 的 UPDATE / DELETE 限本人所写。"""
    facts = _shared(_member(BOB, content=SpaceContentRole.CONTRIBUTOR))
    own = _decide(
        BOB,
        facts=facts,
        entry="update",
        action=SpaceAction.UPDATE,
        author_principal="user:bob",
    )
    others = _decide(
        BOB,
        facts=facts,
        entry="update",
        action=SpaceAction.UPDATE,
        author_principal="user:alice",
    )
    assert own.allowed
    assert not others.allowed
    # editor 不受本人所写限制
    facts_editor = _shared(_member(BOB, content=SpaceContentRole.EDITOR))
    assert _decide(
        BOB,
        facts=facts_editor,
        entry="update",
        action=SpaceAction.UPDATE,
        author_principal="user:alice",
    ).allowed


def test_axis_matrices_do_not_leak_across_axes() -> None:
    """治理轴的 DELETE 指删空间，不因内容轴 editor 而取得。"""
    facts = _shared(_member(BOB, content=SpaceContentRole.EDITOR))
    assert not _decide(
        BOB,
        facts=facts,
        entry="delete_space",
        action=SpaceAction.DELETE,
        axis=SpaceAxis.GOVERNANCE,
    ).allowed


# -- 第 2 组：个体记忆的隔离 ------------------------------------------------ #


def test_2_1_owner_reaches_its_own_space() -> None:
    """本人可达自己的空间：成员表为空，经第 7 步归属对比放行。"""
    outcome = _decide(ALICE)
    assert outcome.allowed
    assert outcome.rule == "owner_comparison"


def test_2_2_agent_reads_what_the_user_wrote_directly() -> None:
    """用户所写内容其名下代理可读。

    判据若退回「作者代理不为空」，用户不经代理写入的条目其代理一律读不到，
    且该失效不报错。
    """
    outcome = _decide(ALICE_VIA_A1, entry="get", author_principal="user:alice")
    assert outcome.allowed


def test_2_3_agents_of_the_same_user_are_mutually_reachable() -> None:
    """同一用户名下各代理互通：代理间的收窄由收窄维标签承担，不由空间隔离。"""
    assert _decide(ALICE_VIA_A1, entry="get", author_principal="user:alice").allowed
    assert _decide(ALICE_VIA_A2, entry="get", author_principal="user:alice").allowed


def test_2_4_autonomous_agent_cannot_read_user_authored_entries() -> None:
    """代理自主运行读不到人写的：作者主体不匹配，且它不覆盖该空间的归属登记。"""
    outcome = _decide(AGENT_A1, entry="get", author_principal="user:alice")
    assert not outcome.allowed
    assert outcome.reason is DenyReason.NOT_COVERED


def test_2_5_owner_may_not_dispose_of_the_space() -> None:
    """归属主体不得处置空间：经代理调删空间、改策略、导出三个都拒绝。"""
    for entry, action, axis in (
        ("delete_space", SpaceAction.DELETE, SpaceAxis.GOVERNANCE),
        ("set_space_policy", SpaceAction.UPDATE, SpaceAxis.GOVERNANCE),
        ("export_space", SpaceAction.READ, SpaceAxis.CONTENT),
    ):
        outcome = _decide(ALICE_VIA_A1, entry=entry, action=action, axis=axis)
        assert not outcome.allowed, entry


def test_2_6_owner_governs_its_own_space_when_calling_directly() -> None:
    """归属主体管得了自己的空间：本人直接调用经归属主体档第一级放行。

    该空间成员表为空，归属主体档是这两条的唯一拦截点。
    """
    updated = _decide(
        ALICE, entry="update_space", action=SpaceAction.UPDATE, axis=SpaceAxis.GOVERNANCE
    )
    policy = _decide(
        ALICE, entry="get_space_policy", action=SpaceAction.READ, axis=SpaceAxis.GOVERNANCE
    )
    assert updated.allowed and updated.rule == "owner_entry_same_dims"
    assert policy.allowed and policy.rule == "owner_entry_covers"


def test_2_7_whole_space_export_is_restricted_to_the_owner_in_person() -> None:
    """整空间导出只对本人：第 7 步对该入口另设排除，落归属主体档第一级。"""
    assert _decide(ALICE, entry="export_space").allowed
    assert not _decide(ALICE_VIA_A1, entry="export_space").allowed


def test_multi_owner_space_blocks_the_first_grade_but_keeps_the_second() -> None:
    """多归属空间：第一级由项数判据挡住，第二级的四个只读入口不受限制。"""
    facts = SpaceAuthorizationFacts(
        owners=(
            Scope(org=ORG, space=SPACE, user="alice"),
            Scope(org=ORG, space=SPACE, user="bob"),
        )
    )
    assert not _decide(
        ALICE,
        facts=facts,
        entry="delete_space",
        action=SpaceAction.DELETE,
        axis=SpaceAxis.GOVERNANCE,
    ).allowed
    assert _decide(
        ALICE,
        facts=facts,
        entry="get_space",
        action=SpaceAction.READ,
        axis=SpaceAxis.GOVERNANCE,
    ).allowed


# -- 判定链的次序与边界 ----------------------------------------------------- #


def test_cross_org_is_rejected_before_scope_coverage() -> None:
    """第 6 步排在第 8 步之前：跨组织即拒绝，主体覆盖为真也不放行。"""
    outcome = _decide(Scope(org="other", user="alice"), scope_covered=True)
    assert not outcome.allowed
    assert outcome.rule == "cross_org"


def test_missing_space_facts_is_denied_not_fetched() -> None:
    """空间级目标缺事实即拒绝，不回落为判定实现自行读取（不变量 2）。"""
    outcome = _decide(ALICE, facts=None)
    assert not outcome.allowed
    assert outcome.reason is DenyReason.CONTEXT_MISMATCH


def test_owner_comparison_never_passes_the_governance_axis() -> None:
    """第 7 步只放行内容轴：治理动作一律转后续步骤。"""
    outcome = _decide(
        ALICE, entry="list_space_members", action=SpaceAction.READ, axis=SpaceAxis.GOVERNANCE
    )
    # 本人直接调用仍可经归属主体档第一级通过，但不得经第 7 步
    assert outcome.rule != "owner_comparison"


def test_owner_entry_precedes_the_not_a_member_rejection() -> None:
    """归属主体档排在「主维无记录即拒绝」之前。

    预建的主空间恒不写成员记录；次序颠倒时该档整体不可达，症状是治理入口静默失效
    而条目读写照常，不产生任何用例失败。
    """
    outcome = _decide(
        ALICE, entry="update_space", action=SpaceAction.UPDATE, axis=SpaceAxis.GOVERNANCE
    )
    assert outcome.allowed
    assert outcome.rule == "owner_entry_same_dims"


def test_explicit_grant_precedes_the_not_a_member_rejection() -> None:
    """显式授权并入之后才判主维无记录：提前则任何显式授权对非成员一律不生效。"""
    facts = _shared(_member(BOB, content=SpaceContentRole.EDITOR))
    denied = _decide(ALICE, facts=facts)
    granted = _decide(ALICE, facts=facts, granted_actions=frozenset({SpaceAction.READ}))
    assert not denied.allowed
    assert granted.allowed


def test_explicit_grant_is_ignored_on_the_governance_axis() -> None:
    """显式授权只在内容轴参与求值：治理权只由成员记录与归属主体档决定。"""
    facts = _shared(_member(BOB, content=SpaceContentRole.EDITOR))
    outcome = _decide(
        ALICE,
        facts=facts,
        entry="add_space_member",
        action=SpaceAction.SHARE,
        axis=SpaceAxis.GOVERNANCE,
        granted_actions=frozenset({SpaceAction.SHARE}),
    )
    assert not outcome.allowed


def test_two_dimensions_are_intersected_not_unioned() -> None:
    """两维取交而非取并：一维给 editor、另一维给 viewer，结果按 viewer 收窄。"""
    facts = _shared(
        _member(Scope(org=ORG, user="alice"), content=SpaceContentRole.EDITOR),
        _member(Scope(org=ORG, agent="a1"), content=SpaceContentRole.VIEWER),
    )
    assert _decide(ALICE_VIA_A1, facts=facts, action=SpaceAction.READ).allowed
    assert not _decide(ALICE_VIA_A1, facts=facts, action=SpaceAction.WRITE).allowed


def test_a_dimension_without_a_matching_record_does_not_narrow() -> None:
    """非主维无记录命中：不构成约束、不参与收窄，不等同于该维给空集。"""
    facts = _shared(_member(Scope(org=ORG, user="alice"), content=SpaceContentRole.EDITOR))
    # agent 维无任何记录命中，结果仍按 user 维的 editor 求值
    assert _decide(ALICE_VIA_A1, facts=facts, action=SpaceAction.WRITE).allowed


def test_primary_dimension_without_a_record_is_rejected() -> None:
    """主维无记录且无显式授权即拒绝。"""
    facts = _shared(_member(Scope(org=ORG, agent="a1"), content=SpaceContentRole.EDITOR))
    outcome = _decide(ALICE_VIA_A1, facts=facts, action=SpaceAction.WRITE)
    assert not outcome.allowed
    assert outcome.rule == "not_a_member"


def test_principal_path_reverses_which_dimension_is_primary() -> None:
    """主维次序取自空间策略；非法值回落默认次序。"""
    facts = _shared(_member(Scope(org=ORG, agent="a1"), content=SpaceContentRole.EDITOR))
    # agent 为主维时该记录即主维记录，通过
    assert _decide(
        ALICE_VIA_A1, facts=facts, action=SpaceAction.WRITE, principal_path="agent_user"
    ).allowed
    # 非法值回落 user_agent，主维 user 无记录，拒绝
    assert not _decide(
        ALICE_VIA_A1, facts=facts, action=SpaceAction.WRITE, principal_path="bogus"
    ).allowed


def test_org_axis_is_not_evaluated_here() -> None:
    """组织级入口由角色闸门终局裁决，不落两轴求值。"""
    outcome = _decide(
        ALICE, entry="create_space", action=SpaceAction.MANAGE_SPACE, axis=SpaceAxis.ORG
    )
    assert not outcome.allowed
    assert outcome.reason is DenyReason.CONTEXT_MISMATCH


# -- 第 9 步 代操作委托：本步只收判定结论，复核由宿主回真源做 ------------------ #
#
# 委托记录的有效期、动作、绑定复核都在宿主侧（见 tests/unit/api/ 的空间级生命周期用例）。
# 本组测的是这一步在判定链里的**位置与适用范围**：它拦得住什么、让位给什么。

_DELEGATION_ALLOW = DecisionOutcome(allowed=True, rule="delegation")
_DELEGATION_DENY = DecisionOutcome(
    allowed=False, rule="delegation_lookup", reason=DenyReason.DELEGATION_INVALID
)


def test_a_delegation_lets_a_non_member_agent_through_on_the_content_axis() -> None:
    """内容轴上委托放行非成员 agent：既不覆盖归属登记、成员表也没有它。

    ``AGENT_A1`` 不带 user 维，第 7 步的作者比对与第 8 步的覆盖都不成立，第 10 步两级
    也都没有它——本用例的通过只能来自第 9 步。
    """
    outcome = _decide(AGENT_A1, delegation=_DELEGATION_ALLOW)
    assert outcome.allowed
    assert outcome.rule == "delegation"


def test_a_failed_delegation_is_terminal_and_does_not_fall_back() -> None:
    """委托失效即终局：不回落第 10 步的成员记录与显式授权。

    回落的后果是审计失真——调用方明说在代操作、代操作凭据已失效，而请求经另一条规则
    通过，「委托失效过」这件事事后无从还原。这里连显式授权一起给足，仍须拒绝。
    """
    outcome = _decide(
        AGENT_A1,
        facts=_shared(_member(AGENT_A1, content=SpaceContentRole.EDITOR)),
        delegation=_DELEGATION_DENY,
        granted_actions=frozenset({SpaceAction.READ}),
    )
    assert not outcome.allowed
    assert outcome.rule == "delegation_lookup"
    assert outcome.reason is DenyReason.DELEGATION_INVALID


def test_the_owner_in_person_is_judged_before_the_delegation_step() -> None:
    """本人直接调用排在委托之前：一条失效委托声明不该拖累本人自己的调用。

    次序若反过来，任何带上失效 ``delegation_id`` 的请求都会被拒——包括归属主体本人在
    自己空间里的读写。
    """
    assert _decide(ALICE, delegation=_DELEGATION_DENY).allowed


def test_the_delegation_step_does_not_apply_to_governance_or_same_dims_entries() -> None:
    """治理轴与「逐维相同」入口不适用委托（与第 7 步同两条排除）。

    这两类入口对归属主体**本人的代理**都是禁止的；委托同样是代理替人操作，适用范围
    必须相同，否则一条内容委托就能改策略、删空间、导出整个空间。

    断言判据名不以 ``delegation`` 开头：本步在这些入口上**整体不参与**，而不是
    「参与了、恰好没通过」——后者会随委托内容变化而翻转。
    """
    governance = _decide(
        AGENT_A1,
        entry="update_space",
        action=SpaceAction.UPDATE,
        axis=SpaceAxis.GOVERNANCE,
        delegation=_DELEGATION_ALLOW,
    )
    assert not governance.allowed
    assert not governance.rule.startswith("delegation")

    export = _decide(AGENT_A1, entry="export_space", delegation=_DELEGATION_ALLOW)
    assert not export.allowed
    assert not export.rule.startswith("delegation")


def test_a_declared_delegation_outside_its_scope_leaves_the_chain_unchanged() -> None:
    """不适用时本步整体让位：结论与「未声明委托」逐字段相同。

    「不适用」与「声明了但没通过」是两件事：前者该当作没声明处理，后者是终局拒绝。
    两者混同会让治理入口的拒绝原因变成 ``delegation_*``，掩盖真实的拒绝理由。
    """
    facts = _shared(_member(BOB, governance=SpaceGovernanceRole.OWNER))
    params = {
        "facts": facts,
        "entry": "update_space",
        "action": SpaceAction.UPDATE,
        "axis": SpaceAxis.GOVERNANCE,
    }
    baseline = _decide(BOB, **params)
    for declared in (_DELEGATION_ALLOW, _DELEGATION_DENY):
        assert _decide(BOB, delegation=declared, **params) == baseline


@pytest.mark.parametrize(
    ("entry", "axis", "applies"),
    [
        ("search", SpaceAxis.CONTENT, True),
        ("add", SpaceAxis.CONTENT, True),
        ("update", SpaceAxis.CONTENT, True),
        ("delete", SpaceAxis.CONTENT, True),
        # 内容轴读动作，但按「逐维相同」裁决：导出物是脱离后续判定的全量副本。
        ("export_space", SpaceAxis.CONTENT, False),
        ("update_space", SpaceAxis.GOVERNANCE, False),
        ("delete_space", SpaceAxis.GOVERNANCE, False),
        ("add_space_member", SpaceAxis.GOVERNANCE, False),
        # 两轴任一的入口在治理轴上同样不适用；内容轴那一次调用才适用。
        ("get_space", SpaceAxis.GOVERNANCE, False),
        ("get_space", SpaceAxis.CONTENT, True),
    ],
)
def test_the_on_behalf_scope_is_the_content_axis_minus_the_same_dims_entries(
    entry: str, axis: SpaceAxis, applies: bool
) -> None:
    """「代人操作」类判据的适用范围：内容轴，扣掉按「逐维相同」裁决的入口。

    第 7 步归属对比与第 9 步代操作委托共用这一份判据，因此它的边界在这里逐项固定：
    两步中任一步私自放宽范围，本组用例即失败。
    """
    assert on_behalf_paths_apply(entry, axis) is applies
