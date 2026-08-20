"""空间感知判定在真实装配下的端到端行为（S09，F03 决策 4「过渡期形态」）。

判据主体的单测在 ``tests/unit/common/security/test_space_decision.py``；本文件测的是
接线：鉴权点取空间事实、折算投影、编排属性，判定宿主取出判定输入并折算结论。用例编号
对应 T01 第 2 组（个体记忆的隔离）。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.api.memory_api_impl.local_memory_api import _first_family_predicate
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.space_roles import (
    SpaceAuthorizationFacts,
    SpaceContentRole,
    SpaceGovernanceRole,
    SpaceMemberFact,
)
from jiuwen_memory.common.type_def import Context, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control import (
    Grant,
    SpaceMember,
    SpacePatch,
    SpacePolicy,
    SpaceSpec,
    SpaceStatus,
)
from jiuwen_memory.control.types import (
    Action,
    MemoryPatch,
    PermissionContext,
    PrincipalPath,
)

pytestmark = pytest.mark.unit

ORG = "acme"
SPACE = "u-alice"
SPACE_SCOPE = Scope(org=ORG, space=SPACE)
SPACE_CONTEXT = Context(scope=SPACE_SCOPE)

# 运维通道：开通服务预建主空间。组织级入口由角色闸门裁决，过渡期无组织级角色，
# 该通道保持改造前的形态（空身份）。
OPS = Scope()
ALICE = Scope(org=ORG, user="alice")
ALICE_VIA_A1 = Scope(org=ORG, user="alice", agent="a1")
ALICE_VIA_A2 = Scope(org=ORG, user="alice", agent="a2")
BOB = Scope(org=ORG, user="bob")


def _kernel():
    """cloud 引擎 + 空间感知判定。

    in_memory 引擎只支持 ``scope.space == ""``，测不了空间级判定。
    """
    engine_params = {
        name: "default"
        for name in (
            "ingestor",
            "index_builder",
            "retriever",
            "kv_store",
            "scheduler",
            "evolver",
            "lifecycle",
        )
    }
    return build_kernel(
        config=Config.from_dict(
            {
                "engine": {"default": {"target": "cloud", "params": engine_params}},
                "permission": {
                    "default": {"target": "space_aware", "params": {"db_path": ":memory:"}}
                },
            }
        )
    )


@pytest.fixture
def api():
    kernel = _kernel()
    kernel.api.create_space(
        SpaceSpec(org=ORG, space=SPACE, owner=ALICE), identity=OPS
    )
    return kernel.api


def test_the_decision_implementation_is_assembled(api) -> None:
    """装配落到空间感知判定，且它向鉴权点声明需要空间事实。"""
    assert type(api._perm).__name__ == "SpaceAwarePermissionManager"
    assert api._perm.requires_space_facts() is True
    assert api._membership is not None


def test_owner_registration_lands_and_is_read_back(api) -> None:
    """归属登记落盘：判定的第一项输入。"""
    info = api.get_space(ORG, SPACE, identity=ALICE)
    assert [owner.user for owner in info.owners] == ["alice"]


def test_2_1_owner_writes_and_reads_its_own_space(api) -> None:
    """本人可达自己的空间：成员表为空，经归属对比放行。"""
    units = api.add("alice 偏好深色主题", SPACE_SCOPE, identity=ALICE)
    assert units
    result = api.search("深色主题", SPACE_CONTEXT, identity=ALICE, top_k=5)
    assert result.items


def test_2_2_and_2_3_agents_of_the_same_user_reach_what_the_user_wrote(api) -> None:
    """用户所写内容其名下代理可读，且换代理不改变可达性。

    判据若退回「作者代理不为空」，用户不经代理直接写入的条目其代理一律读不到，
    且该失效不报错。
    """
    api.add("alice 偏好深色主题", SPACE_SCOPE, identity=ALICE)
    for actor in (ALICE_VIA_A1, ALICE_VIA_A2):
        assert api.search("深色主题", SPACE_CONTEXT, identity=actor, top_k=5).items


def test_another_user_cannot_reach_the_space(api) -> None:
    """他人不可达：既不覆盖归属登记，成员表也为空。"""
    api.add("alice 偏好深色主题", SPACE_SCOPE, identity=ALICE)
    with pytest.raises(PermissionDeniedError):
        api.search("深色主题", SPACE_CONTEXT, identity=BOB, top_k=5)


def test_2_6_owner_governs_its_own_space_in_person(api) -> None:
    """归属主体管得了自己的空间：本人直接调用经归属主体档第一级放行。

    该空间成员表为空，归属主体档是这条路径的唯一拦截点——次序若排在「主维无记录
    即拒绝」之后，本用例失败而条目读写照常通过。
    """
    info = api.update_space(ORG, SPACE, SpacePatch(display_name="Alice"), identity=ALICE)
    assert info.display_name == "Alice"


def test_2_5_owner_may_not_dispose_of_the_space_through_an_agent(api) -> None:
    """归属主体不得处置空间：经代理调用治理入口一律拒绝。"""
    with pytest.raises(PermissionDeniedError):
        api.update_space(ORG, SPACE, SpacePatch(display_name="X"), identity=ALICE_VIA_A1)


def test_2_7_whole_space_export_is_restricted_to_the_owner_in_person(api) -> None:
    """整空间导出只对本人：判定第 7 步对该入口另设排除，落归属主体档第一级。

    导出物是脱离后续判定的全量副本；本步不排除时归属主体的代理即可导出整个空间。
    """
    api.add("alice 偏好深色主题", SPACE_SCOPE, identity=ALICE)
    assert api.export_space(ORG, SPACE, identity=ALICE) is not None
    with pytest.raises(PermissionDeniedError):
        api.export_space(ORG, SPACE, identity=ALICE_VIA_A1)


def test_space_metadata_is_readable_through_an_agent(api) -> None:
    """归属主体档第二级：覆盖即可，用户经其代理读空间元数据通过。"""
    assert api.get_space(ORG, SPACE, identity=ALICE_VIA_A1) is not None


def test_an_identity_without_a_principal_dimension_is_rejected_on_space_entries(api) -> None:
    """空间级入口的形态校验：主体维全空即拒绝。

    组织级入口不受此限——运维通道正是靠该形态建空间，见 fixture。
    """
    with pytest.raises(PermissionDeniedError):
        api.get_space(ORG, SPACE, identity=Scope(org=ORG))


# -- 第 1 组：空间内的权限分档，与写入路径的三处防护 ------------------------ #


CAROL = Scope(org=ORG, user="carol")
DAVE = Scope(org=ORG, user="dave")


def _member(user: str, content: SpaceContentRole, governance: SpaceGovernanceRole):
    return SpaceMember(
        scope=Scope(user=user), content_role=content, governance_role=governance
    )


def test_owner_adds_the_first_member_and_that_member_can_read(api) -> None:
    """归属主体给自己的个体空间加第一个成员。

    此刻成员表为空、两维都无记录，授予上界的基数若只按成员记录计即为空档，个体空间
    永远无法转为共享空间——基数函数对这一形态另有分支。
    """
    api.add("alice 偏好深色主题", SPACE_SCOPE, identity=ALICE)
    api.add_space_member(
        ORG, SPACE, _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        identity=ALICE,
    )
    assert api.search("深色主题", SPACE_CONTEXT, identity=BOB, top_k=5).items


def test_1_2_a_content_editor_cannot_manage_members(api) -> None:
    """能读写内容、管不了成员：内容轴 editor + 治理轴 none。"""
    api.add_space_member(
        ORG, SPACE, _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        identity=ALICE,
    )
    with pytest.raises(PermissionDeniedError):
        api.add_space_member(
            ORG, SPACE, _member("carol", SpaceContentRole.VIEWER, SpaceGovernanceRole.NONE),
            identity=BOB,
        )


def test_1_1_a_governance_manager_cannot_read_content(api) -> None:
    """能管成员、看不到内容：治理轴 manager + 内容轴 none。"""
    api.add("alice 偏好深色主题", SPACE_SCOPE, identity=ALICE)
    api.add_space_member(
        ORG, SPACE, _member("carol", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        identity=ALICE,
    )
    api.add_space_member(
        ORG, SPACE, _member("dave", SpaceContentRole.VIEWER, SpaceGovernanceRole.NONE),
        identity=CAROL,
    )
    with pytest.raises(PermissionDeniedError):
        api.search("深色主题", SPACE_CONTEXT, identity=CAROL, top_k=5)


def test_governance_ceiling_blocks_appointing_an_owner(api) -> None:
    """治理轴授予上界：管理员可增设管理员，不能设立拥有者。"""
    api.add_space_member(
        ORG, SPACE, _member("carol", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        identity=ALICE,
    )
    api.add_space_member(
        ORG, SPACE, _member("dave", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        identity=CAROL,
    )
    with pytest.raises(PermissionDeniedError):
        api.add_space_member(
            ORG, SPACE, _member("dave", SpaceContentRole.NONE, SpaceGovernanceRole.OWNER),
            identity=CAROL,
        )


def test_removal_ceiling_blocks_removing_a_higher_grade_member(api) -> None:
    """改前值同受上界约束：管理员不能移除拥有者。"""
    api.add_space_member(
        ORG, SPACE, _member("carol", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        identity=ALICE,
    )
    with pytest.raises(PermissionDeniedError):
        api.remove_space_member(ORG, SPACE, Scope(org=ORG, space=SPACE, user="alice"),
                                identity=CAROL)


def test_1_5_a_member_record_must_not_raise_the_callers_own_grade(api) -> None:
    """不能给自己提权，且约束覆盖两轴。

    只禁治理轴的话，治理管理员一次加成员即可把自己的内容档设为可编辑——「能管成员而
    看不到内容」那一档一次调用即失效。
    """
    api.add_space_member(
        ORG, SPACE, _member("carol", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        identity=ALICE,
    )
    with pytest.raises(PermissionDeniedError):
        api.add_space_member(
            ORG, SPACE, _member("carol", SpaceContentRole.EDITOR, SpaceGovernanceRole.MANAGER),
            identity=CAROL,
        )


def test_1_6_configuring_someone_else_is_not_self_promotion(api) -> None:
    """能给别人配内容档：约束的是自提，不是代他人配置。"""
    api.add_space_member(
        ORG, SPACE, _member("carol", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        identity=ALICE,
    )
    api.add_space_member(
        ORG, SPACE, _member("dave", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        identity=CAROL,
    )
    assert any(m.scope.user == "dave" for m in api.list_space_members(ORG, SPACE, identity=ALICE))


def test_1_4_downgrade_takes_effect_immediately(api) -> None:
    """降权即时生效：移除成员后立即以该成员访问即被拒，不必等缓存过期。

    治理写入不下发事实缓存失效时，被移除的成员在 TTL 内仍按旧快照通过判定，而该窗口
    内的放行既无异常也无审计差异。
    """
    api.add("alice 偏好深色主题", SPACE_SCOPE, identity=ALICE)
    api.add_space_member(
        ORG, SPACE, _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        identity=ALICE,
    )
    assert api.search("深色主题", SPACE_CONTEXT, identity=BOB, top_k=5).items
    api.remove_space_member(ORG, SPACE, Scope(org=ORG, space=SPACE, user="bob"), identity=ALICE)
    with pytest.raises(PermissionDeniedError):
        api.search("深色主题", SPACE_CONTEXT, identity=BOB, top_k=5)


def test_member_scope_normalisation_matches_the_space_manager(api) -> None:
    """防护侧的成员 scope 归一化与空间管理器的写入侧一致。

    两侧分叉时自提比对的 org 维一空一有值，逐维相同恒不成立——自提禁止整条失效，
    且不报错、不留审计差异。
    """
    api.add_space_member(
        ORG, SPACE, _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        identity=ALICE,
    )
    stored = [m.scope for m in api.list_space_members(ORG, SPACE, identity=ALICE)]
    guarded = api._normalized_member_scope(SPACE_SCOPE, Scope(user="bob"))
    assert guarded in stored


# -- 检索谓词第一族（S09「检索两族谓词」） --------------------------------- #


def _ctx(owners, members=()):
    return PermissionContext(
        scope=SPACE_SCOPE,
        space_facts=SpaceAuthorizationFacts(owners=tuple(owners), members=tuple(members)),
    )


def test_first_family_is_empty_for_a_user_and_for_its_agents() -> None:
    """用户本人直接调用，或经其名下代理调用：不追加，该空间内全部条目可见。"""
    ctx = _ctx([Scope(org=ORG, space=SPACE, user="alice")])
    assert _first_family_predicate(ALICE, ctx) == []
    assert _first_family_predicate(ALICE_VIA_A1, ctx) == []


def test_first_family_narrows_an_autonomous_agent_to_its_own_entries() -> None:
    """代理自主运行：追加 ``author_principal == "agent:<id>"``。

    与判定链第 7 步的作者比对同源——判据分叉即出现「搜不到但按 id 读得到」或其反向。
    """
    ctx = _ctx([Scope(org=ORG, space="a-a1", agent="a1")])
    clauses = _first_family_predicate(Scope(org=ORG, agent="a1"), ctx)
    assert [(c.field, c.value) for c in clauses] == [
        ("system_metadata.author_principal", "agent:a1")
    ]


def test_first_family_always_narrows_a_multi_owner_space() -> None:
    """多归属空间恒追加「作者主体等于调用方」，不看调用方形态。

    缺它则回填窗口内两个归属者互相召回得到对方的条目，且不报错。
    """
    ctx = _ctx(
        [
            Scope(org=ORG, space=SPACE, user="alice"),
            Scope(org=ORG, space=SPACE, user="bob"),
        ]
    )
    clauses = _first_family_predicate(ALICE, ctx)
    assert [(c.field, c.value) for c in clauses] == [
        ("system_metadata.author_principal", "user:alice")
    ]
    # 经其名下代理调用推导出同一个作者主体，谓词一致
    assert _first_family_predicate(ALICE_VIA_A1, ctx)[0].value == "user:alice"


def test_first_family_does_not_apply_to_a_collaborative_space() -> None:
    """仅个体空间生效：协作空间的可见范围由两轴角色裁决。

    按作者收窄会使协作空间失去协作意义——成员互相看不到对方写的内容。
    """
    ctx = _ctx(
        [],
        [
            SpaceMemberFact(
                scope=Scope(org=ORG, space=SPACE, user="alice"),
                content_role=SpaceContentRole.EDITOR,
                governance_role=SpaceGovernanceRole.NONE,
            )
        ],
    )
    assert _first_family_predicate(ALICE, ctx) == []


def test_first_family_is_empty_without_space_facts() -> None:
    """未装配空间级判定时不生成谓词，行为与改造前一致。"""
    assert _first_family_predicate(ALICE, None) == []
    assert _first_family_predicate(ALICE, PermissionContext(scope=SPACE_SCOPE)) == []


# -- 空间状态校验（S09「空间状态校验」） ----------------------------------- #


def test_archived_space_allows_reads_and_rejects_writes(api) -> None:
    """归档空间：读动作放行，写动作拒绝，错误类型是参数校验失败而非权限拒绝。"""
    api.add("alice 偏好深色主题", SPACE_SCOPE, identity=ALICE)
    api.archive_space(ORG, SPACE, identity=ALICE)

    assert api.search("深色主题", SPACE_CONTEXT, identity=ALICE, top_k=5) is not None
    with pytest.raises(ValidationError):
        api.add("另一条", SPACE_SCOPE, identity=ALICE)


def test_state_check_runs_after_authorization_so_the_two_errors_stay_distinguishable(
    api,
) -> None:
    """无权调用方得权限拒绝，有权调用方对归档空间得参数校验失败。

    次序若颠倒，无权调用方也能凭错误类型判断出该空间处于归档——与「不泄露空间是否
    存在」的方向相反。该次序被改动不会使其他用例失败，因此由本用例固定。
    """
    api.archive_space(ORG, SPACE, identity=ALICE)
    with pytest.raises(PermissionDeniedError):
        api.add("bob 写入", SPACE_SCOPE, identity=BOB)
    with pytest.raises(ValidationError):
        api.add("alice 写入", SPACE_SCOPE, identity=ALICE)


def test_list_spaces_evaluates_each_candidate_space(api) -> None:
    """``list_spaces`` 逐空间求值，无权的直接剔除、不报错。

    走与单空间入口同一个鉴权方法——分叉即出现「列得出但打不开」或其反向。
    """
    api.create_space(SpaceSpec(org=ORG, space="u-bob", owner=BOB), identity=OPS)
    alice_visible = {info.space for info in api.list_spaces(ORG, identity=ALICE)}
    bob_visible = {info.space for info in api.list_spaces(ORG, identity=BOB)}
    assert alice_visible == {SPACE}
    assert bob_visible == {"u-bob"}


def test_list_spaces_does_not_truncate_candidates_before_authorization(api) -> None:
    """``limit`` 在鉴权之后生效，不截候选（R06 D2）。

    在鉴权之前截断时，可读空间字典序靠后即被挡在候选之外：本用例里 alice 唯一的空间排在
    十二个他人空间之后，``limit=5`` 的候选全是他人空间，过滤后返回空。失效形态是静默空
    返回，与本规约其余各处「截断记 WARNING、通道失败进 errors」的口径相反。
    """
    for index in range(12):
        api.create_space(SpaceSpec(org=ORG, space=f"a-other-{index:02d}", owner=BOB), identity=OPS)
    # 前提：全库扫描的前五个里没有 alice 的空间，返回条数因而取决于截断次序。
    assert SPACE not in {info.space for info in api._space.list(ORG, limit=5)}
    assert [info.space for info in api.list_spaces(ORG, identity=ALICE, limit=5)] == [SPACE]


def test_list_spaces_applies_limit_after_authorization(api) -> None:
    """``limit`` 是返回条数上限，不是候选条数上限（S09「翻页语义变更」）。"""
    for index in range(4):
        space = f"p-shared-{index}"
        api.create_space(SpaceSpec(org=ORG, space=space, owner=ALICE), identity=OPS)
    assert len(api.list_spaces(ORG, identity=ALICE)) == 5
    assert len(api.list_spaces(ORG, identity=ALICE, limit=2)) == 2


def test_list_spaces_ignores_the_cursor_but_records_it(api) -> None:
    """``cursor`` 标记废弃：候选来自反查索引，全库偏移量无从解释。

    忽略但记进审计明细——静默忽略会让期望翻页的调用方拿到重复页而无从察觉。
    """
    assert [info.space for info in api.list_spaces(ORG, identity=ALICE, cursor="3")] == [SPACE]


def test_list_spaces_lists_a_space_reachable_only_through_an_explicit_grant(api) -> None:
    """靠显式授权取得读权的空间必须列得出（R06 复核 D6）。

    候选若取主体反查索引，这一条必然失败：索引的写入方只有归属登记与成员记录两类，
    ``grant`` 不写索引。失效形态是「直接 ``search`` 读得到、``list_spaces`` 列不出来」，
    且不报错。与 F03 决策 23「不以反查索引粗筛写入候选」是同一条理由，读侧同样成立。
    """
    api.create_space(SpaceSpec(org=ORG, space="p-x", owner=ALICE), identity=OPS)
    api.add_space_member(
        ORG,
        "p-x",
        SpaceMember(
            scope=BOB,
            content_role=SpaceContentRole.EDITOR,
            governance_role=SpaceGovernanceRole.OWNER,
        ),
        identity=ALICE,
    )
    dave = Scope(org=ORG, user="dave")
    api.grant(
        Grant(grantor=Scope(org=ORG, space="p-x"), grantee=dave, actions=[Action.READ]),
        identity=BOB,
    )
    assert "p-x" not in api._membership.spaces_for(dave, ORG)
    assert [info.space for info in api.list_spaces(ORG, identity=dave)] == ["p-x"]


def test_list_spaces_rejects_a_non_positive_limit(api) -> None:
    """``limit <= 0`` 在两条路径上都是 ValidationError，不静默返回空。"""
    with pytest.raises(ValidationError):
        api.list_spaces(ORG, identity=ALICE, limit=0)


def test_list_second_stage_strips_the_author_mark(api) -> None:
    """``list`` 第二段不携带作者标记（S09）。

    条目权限上下文由引擎按条目 metadata 整体构造，作者标记因此自动在内，必须显式剥掉：
    逐条鉴权的失败形态是抛异常而非过滤，携带后个体空间内只要有一条作者不是调用方的条目，
    整次调用即失败。内容边界改由第一族谓词在取数时承担。
    """
    unit_context = PermissionContext(
        resource_type="memory_unit",
        scope=SPACE_SCOPE,
        metadata={"author_principal": "user:bob"},
    )
    carried, _ = api._apply_space_policy_context(SPACE_SCOPE, unit_context, entry="list")
    stripped, _ = api._apply_space_policy_context(
        SPACE_SCOPE, unit_context, entry="list", carry_author_marks=False
    )
    assert carried.metadata.get("author_principal") == "user:bob"
    assert "author_principal" not in stripped.metadata


def test_contributor_may_change_its_own_entry_but_not_others(api) -> None:
    """只能改自己写的：两段鉴权合起来实现该判据（T01 1-3）。

    第一段的目标是空间、不带条目信息，此时「本人所写」附加集合按可能成立处置，否则
    ``contributor`` 在第一段即被拒；最终边界由第二段的实际作者比对给出。
    """
    api.add_space_member(
        ORG, SPACE, _member("bob", SpaceContentRole.CONTRIBUTOR, SpaceGovernanceRole.NONE),
        identity=ALICE,
    )
    alice_unit = api.add("alice 写的", SPACE_SCOPE, identity=ALICE)[0]
    bob_unit = api.add("bob 写的", SPACE_SCOPE, identity=BOB)[0]

    api.update(bob_unit.id, SPACE_SCOPE, MemoryPatch(content="bob 改自己的"), identity=BOB)
    with pytest.raises(PermissionDeniedError):
        api.update(alice_unit.id, SPACE_SCOPE, MemoryPatch(content="bob 改别人的"), identity=BOB)


# -- 演进模式与任务入口的动作取值（S09「入口到轴与动作的映射」） ------------- #


def test_forget_and_consolidate_are_denied_to_a_contributor(api) -> None:
    """去重与遗忘取 ``UPDATE`` 且不放宽到「本人所写」。

    两种模式改写既有条目、作用对象是整个空间，而可贡献档的 ``UPDATE`` 限本人所写。
    默认动作 ``WRITE`` 若不被覆盖，可贡献档成员即可对他人写入的条目执行遗忘。
    """
    api.add_space_member(
        ORG, SPACE, _member("bob", SpaceContentRole.CONTRIBUTOR, SpaceGovernanceRole.NONE),
        identity=ALICE,
    )
    api.evolve(SPACE_SCOPE, EvolveMode.EXTRACT, identity=BOB)
    for mode in (EvolveMode.FORGET, EvolveMode.CONSOLIDATE):
        with pytest.raises(PermissionDeniedError):
            api.evolve(SPACE_SCOPE, mode, identity=BOB)


def test_forget_stays_open_to_the_owner(api) -> None:
    """收紧只针对可贡献档：归属主体本人不受影响。"""
    assert api.evolve(SPACE_SCOPE, EvolveMode.FORGET, identity=ALICE)


def test_job_entries_take_the_action_of_the_mode_that_started_the_job(api) -> None:
    """任务状态查询与取消按发起该作业的演进模式取动作。

    取值来自 ``JobInfo.mode``；作业以遗忘模式发起时，查询与取消同样落 ``UPDATE``。
    """
    api.add_space_member(
        ORG, SPACE, _member("bob", SpaceContentRole.CONTRIBUTOR, SpaceGovernanceRole.NONE),
        identity=ALICE,
    )
    extract_job = api.evolve(SPACE_SCOPE, EvolveMode.EXTRACT, identity=BOB)
    forget_job = api.evolve(SPACE_SCOPE, EvolveMode.FORGET, identity=ALICE)

    api.job_status(extract_job, identity=BOB)
    with pytest.raises(PermissionDeniedError):
        api.job_status(forget_job, identity=BOB)
    with pytest.raises(PermissionDeniedError):
        api.job_cancel(forget_job, identity=BOB)


# -- 建空间与事实缓存 --------------------------------------------------------- #


def test_a_space_read_before_it_exists_does_not_block_the_first_write(api) -> None:
    """建空间下发事实缓存失效：先查后建是常见形态。

    事实缓存对「空间不存在」同样装填一份（元数据与成员皆空）。不清则新空间在一个 TTL 内
    判定无归属、无成员，归属主体本人也写不进去，且无任何错误信号可循。
    """
    with pytest.raises(PermissionDeniedError):
        api.get_space(ORG, "u-new", identity=ALICE)
    api.create_space(SpaceSpec(org=ORG, space="u-new", owner=ALICE), identity=OPS)
    assert api.add("建成即可写", Scope(org=ORG, space="u-new"), identity=ALICE)


# -- 冻结与归档下的可变更范围 ------------------------------------------------- #


def test_a_frozen_space_rejects_changes_other_than_status(api) -> None:
    """冻结态仅放行只改状态的那一次变更。

    判据取 ``SpacePatch`` 的内容而非入口名：``update_space`` 同时能改 ``policy`` 与
    ``principal_path``，二者都是判定依据，只看入口名等于允许在冻结态改写判定依据。
    """
    api.update_space(ORG, SPACE, SpacePatch(status=SpaceStatus.FROZEN), identity=ALICE)
    with pytest.raises(ValidationError):
        api.update_space(ORG, SPACE, SpacePatch(display_name="冻结期改名"), identity=ALICE)
    with pytest.raises(ValidationError):
        api.update_space(
            ORG, SPACE, SpacePatch(policy=SpacePolicy(require_space=True)), identity=ALICE
        )
    info = api.update_space(ORG, SPACE, SpacePatch(status=SpaceStatus.ACTIVE), identity=ALICE)
    assert info.status is SpaceStatus.ACTIVE


# -- 空间策略裁剪（S09「空间策略必须从元数据返回值中裁剪」） ------------------ #


def _with_quota(api, identity):
    api.set_space_policy(ORG, SPACE, SpacePolicy(quotas={"max_units": "10"}), identity=identity)


def test_policy_is_trimmed_for_a_caller_who_passes_only_the_content_axis(api) -> None:
    """只经内容轴通过则策略置空，经治理轴通过可读策略。

    只把 ``get_space_policy`` 归治理轴而不裁剪 ``get_space`` 的返回值，调用方改调后者
    即可照样读走策略。
    """
    _with_quota(api, ALICE)
    api.add_space_member(
        ORG, SPACE, _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        identity=ALICE,
    )
    assert api.get_space(ORG, SPACE, identity=ALICE).policy.quotas == {"max_units": "10"}
    assert api.get_space(ORG, SPACE, identity=BOB).policy.quotas == {}
    with pytest.raises(PermissionDeniedError):
        api.get_space_policy(ORG, SPACE, identity=BOB)


def test_list_spaces_trims_the_policy_by_the_same_rule(api) -> None:
    """``list_spaces`` 与 ``get_space`` 同判据同实现。

    分叉即出现「列表里读得到、单查读不到」或其反向。
    """
    _with_quota(api, ALICE)
    api.add_space_member(
        ORG, SPACE, _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        identity=ALICE,
    )
    listed = {info.space: info for info in api.list_spaces(ORG, identity=BOB)}
    assert listed[SPACE].policy.quotas == {}


def test_trimming_keeps_the_principal_path_that_the_top_level_field_already_exposes(
    api,
) -> None:
    """``principal_path`` 的两份镜像裁剪后仍一致。

    该值在 :class:`SpaceInfo` 上有顶层字段与策略内字段两份，空间管理器同步写两份。
    裁剪整体替换策略对象，若不保留这一项，读策略内那份得到的取值与顶层字段相反——
    顶层字段并未被裁剪，遮不住却先自相矛盾。
    """
    api.set_space_policy(
        ORG,
        SPACE,
        SpacePolicy(quotas={"max_units": "10"}, principal_path=PrincipalPath.AGENT_USER),
        identity=ALICE,
    )
    api.add_space_member(
        ORG, SPACE, _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        identity=ALICE,
    )
    trimmed = api.get_space(ORG, SPACE, identity=BOB)
    assert trimmed.principal_path is PrincipalPath.AGENT_USER
    assert trimmed.policy.principal_path is trimmed.principal_path
    # 其余字段照常裁剪：保留是针对这一项的，不是不裁剪了。
    assert trimmed.policy.quotas == {}


def test_a_grant_whose_grantor_has_no_space_dimension_is_not_blocked_by_the_guard(
    api,
) -> None:
    """授出上界防护对不涉及空间的授权不适用。

    三处防护中只有它的目标由调用方提供。缺 space 维时若照常去读空间事实，空间管理器
    的入参校验异常会穿过防护抛给调用方——而调用方没有涉及任何空间。覆盖判定要求两侧
    space 维相同，这条授权本就触达不到任何空间。
    """
    grant = Grant(grantor=ALICE, grantee=BOB, actions=(Action.READ,))
    api.grant(grant, identity=ALICE)
    assert api.revoke(grant, identity=ALICE) is None


def test_the_passing_axis_is_recorded_in_the_audit_detail(api) -> None:
    """通过的轴落审计：裁剪判据须可追溯到具体一次调用。"""
    api.get_space(ORG, SPACE, identity=ALICE)
    axes = [
        event.detail.get("permission_axis")
        for event in api._audit.query({"action": "get_space"})
    ]
    assert axes and set(axes) == {"governance"}


# -- 状态校验与判定共用同一份快照 --------------------------------------------- #


def test_state_check_reuses_the_facts_read_by_authorization(api, monkeypatch) -> None:
    """装配空间级判定后，状态校验与写入前置校验都不再独立点读空间元数据。

    独立点读有两项代价：鉴权路径上多一次后端读，且状态与判定事实取自不同快照。把
    独立点读改成抛异常，本用例即固定「该路径不再被走到」。
    """
    def _must_not_be_called(_scope):
        raise AssertionError("空间元数据应取自本次鉴权已读的事实，不另发起点读")

    monkeypatch.setattr(type(api), "_space_info_if_exists", staticmethod(_must_not_be_called))
    assert api.add("不触发独立点读", SPACE_SCOPE, identity=ALICE)
    api.get_space(ORG, SPACE, identity=ALICE)
