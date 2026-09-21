"""空间感知判定在真实装配下的端到端行为（F07 决策 4「过渡期形态」）。

判据主体的单测在 ``tests/unit/common/security/test_space_decision.py``；本文件测的是
接线：鉴权点取空间事实、折算投影、编排属性，判定宿主取出判定输入并折算结论。覆盖面是
个体记忆的隔离——用户主空间与协作空间的可见范围互不越界。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.api.memory_api_impl.local_memory_api import _first_family_predicate
from jiuwen_memory.common.errors import AuthenticationError, PermissionDeniedError, ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.authorization.authorization_impl.standard_authorizer import (
    _delegation_sources_for,
)
from jiuwen_memory.common.security.space_roles import (
    SpaceAuthorizationFacts,
    SpaceContentRole,
    SpaceGovernanceRole,
    SpaceMemberFact,
)
from jiuwen_memory.common.security.types import Delegation, Role
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
from tests.support.scoped_authenticator import ScopedAuthenticator
from tests.unit.api.fixtures import security_for

pytestmark = pytest.mark.unit

ORG = "acme"
SPACE = "u-alice"
SPACE_SCOPE = Scope(org=ORG, space=SPACE)
SPACE_CONTEXT = Context(scope=SPACE_SCOPE)

# 运维通道：开通服务预建主空间。组织级入口由角色闸门裁决，过渡期无组织级角色，
# 该通道保持改造前的形态（空身份）。
# 运维主体：建测试空间用。必须具名——PR2 起空 Scope 不再是特权形态，判定实现
# 的第 2 步直接拒（``empty_actor``），「没填内容的身份即平台管理员」那条线已断。
OPS = Scope(org=ORG, user="ops")
ALICE = Scope(org=ORG, user="alice")
ALICE_VIA_A1 = Scope(org=ORG, user="alice", agent="a1")
ALICE_VIA_A2 = Scope(org=ORG, user="alice", agent="a2")
BOB = Scope(org=ORG, user="bob")


_ENGINE_COMPONENT_NAMES = (
    "ingestor",
    "index_builder",
    "retriever",
    "kv_store",
    "scheduler",
    "evolver",
    "lifecycle",
)

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为


def _kernel():
    """cloud 引擎 + 空间感知判定。

    in_memory 引擎只支持 ``scope.space == ""``，测不了空间级判定。
    """
    engine_params = {name: "default" for name in _ENGINE_COMPONENT_NAMES}
    return build_kernel(
        config=Config.from_dict(
            {
                "engine": {"default": {"target": "cloud", "params": engine_params}},
                "permission": {
                    "default": {"target": "space_aware", "params": {"db_path": ":memory:"}}
                },
                "authorizer": {
                    "default": {
                        "target": "space_aware",
                        "params": {"delegate": "standard"},
                    },
                    "standard": {
                        "target": "standard",
                        "params": {"grant_store": "default", "delegation_store": "default"},
                    },
                },
                "grant_store": {"default": "memory"},
                "delegation_store": {"default": "memory"},
            }
        )
    )


@pytest.fixture
def api():
    kernel = _kernel()
    kernel.api.create_space(SpaceSpec(org=ORG, space=SPACE, owner=ALICE), security=SEC_OPS)
    return kernel.api


def test_the_decision_implementation_is_assembled(api) -> None:
    """装配落到空间感知判定，且它向鉴权点声明需要空间事实。"""
    assert type(api._perm).__name__ == "SpaceAwarePermissionManager"
    assert api._perm.requires_space_facts() is True
    assert api._membership is not None


def test_owner_registration_lands_and_is_read_back(api) -> None:
    """归属登记落盘：判定的第一项输入。"""
    info = api.get_space(ORG, SPACE, security=SEC_ALICE)
    assert [owner.user for owner in info.owners] == ["alice"]


def test_2_1_owner_writes_and_reads_its_own_space(api) -> None:
    """本人可达自己的空间：成员表为空，经归属对比放行。"""
    units = api.add("alice 偏好深色主题", SPACE_SCOPE, security=SEC_ALICE)
    assert units
    result = api.search("深色主题", SPACE_CONTEXT, security=SEC_ALICE, top_k=5)
    assert result.items


def test_2_2_and_2_3_agents_of_the_same_user_reach_what_the_user_wrote(api) -> None:
    """用户所写内容其名下代理可读，且换代理不改变可达性。

    判据若退回「作者代理不为空」，用户不经代理直接写入的条目其代理一律读不到，
    且该失效不报错。
    """
    api.add("alice 偏好深色主题", SPACE_SCOPE, security=SEC_ALICE)
    for actor in (ALICE_VIA_A1, ALICE_VIA_A2):
        assert api.search(
            "深色主题",
            SPACE_CONTEXT,
            security=security_for(api, actor),
            top_k=5,
        ).items


def test_another_user_cannot_reach_the_space(api) -> None:
    """他人不可达：既不覆盖归属登记，成员表也为空。"""
    api.add("alice 偏好深色主题", SPACE_SCOPE, security=SEC_ALICE)
    with pytest.raises(PermissionDeniedError):
        api.search("深色主题", SPACE_CONTEXT, security=SEC_BOB, top_k=5)


def test_2_6_owner_governs_its_own_space_in_person(api) -> None:
    """归属主体管得了自己的空间：本人直接调用经归属主体档第一级放行。

    该空间成员表为空，归属主体档是这条路径的唯一拦截点——次序若排在「主维无记录
    即拒绝」之后，本用例失败而条目读写照常通过。
    """
    info = api.update_space(ORG, SPACE, SpacePatch(display_name="Alice"), security=SEC_ALICE)
    assert info.display_name == "Alice"


def test_2_5_owner_may_not_dispose_of_the_space_through_an_agent(api) -> None:
    """归属主体不得处置空间：经代理调用治理入口一律拒绝。"""
    with pytest.raises(PermissionDeniedError):
        api.update_space(
            ORG, SPACE, SpacePatch(display_name="X"), security=security_for(api, ALICE_VIA_A1)
        )


def test_2_7_whole_space_export_is_restricted_to_the_owner_in_person(api) -> None:
    """整空间导出只对本人：判定第 7 步对该入口另设排除，落归属主体档第一级。

    导出物是脱离后续判定的全量副本；本步不排除时归属主体的代理即可导出整个空间。
    """
    api.add("alice 偏好深色主题", SPACE_SCOPE, security=SEC_ALICE)
    assert api.export_space(ORG, SPACE, security=SEC_ALICE) is not None
    with pytest.raises(PermissionDeniedError):
        api.export_space(ORG, SPACE, security=security_for(api, ALICE_VIA_A1))


def test_space_metadata_is_readable_through_an_agent(api) -> None:
    """归属主体档第二级：覆盖即可，用户经其代理读空间元数据通过。"""
    assert api.get_space(ORG, SPACE, security=security_for(api, ALICE_VIA_A1)) is not None


def test_an_identity_without_a_principal_dimension_is_rejected_on_space_entries(api) -> None:
    """所有受控入口都拒绝无主体身份，运维须使用具名 ROOT/ADMIN。"""
    with pytest.raises(AuthenticationError):
        api.get_space(ORG, SPACE, security=internal_context(ScopedAuthenticator(Scope(org=ORG))))


# -- 第 1 组：空间内的权限分档，与写入路径的三处防护 ------------------------ #


CAROL = Scope(org=ORG, user="carol")
DAVE = Scope(org=ORG, user="dave")

# 接口先行过渡桥接：identity Scope 包成 RequestSecurityContext（安全实装合入后随接口一并改）
SEC_ALICE = internal_context(ScopedAuthenticator(ALICE))
SEC_BOB = internal_context(ScopedAuthenticator(BOB))
SEC_CAROL = internal_context(ScopedAuthenticator(CAROL))
# 建空间走管理面 MANAGE_SPACE 闸门，闸门读的是服务端 role，不看 actor 的 Scope 形状——
# 过渡件默认给 USER（它有生产调用点，默认 ROOT 会把每个认证请求提到最高权限），
# 运维档要在调用点显式写出。ADMIN 即够：目标 space 带 org，管辖止于本 org。
SEC_OPS = internal_context(ScopedAuthenticator(OPS, role=Role.ADMIN))


def _member(user: str, content: SpaceContentRole, governance: SpaceGovernanceRole):
    return SpaceMember(scope=Scope(user=user), content_role=content, governance_role=governance)


def test_owner_adds_the_first_member_and_that_member_can_read(api) -> None:
    """归属主体给自己的个体空间加第一个成员。

    此刻成员表为空、两维都无记录，授予上界的基数若只按成员记录计即为空档，个体空间
    永远无法转为共享空间——基数函数对这一形态另有分支。
    """
    api.add("alice 偏好深色主题", SPACE_SCOPE, security=SEC_ALICE)
    api.add_space_member(
        ORG,
        SPACE,
        _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        security=SEC_ALICE,
    )
    assert api.search("深色主题", SPACE_CONTEXT, security=SEC_BOB, top_k=5).items


def test_1_2_a_content_editor_cannot_manage_members(api) -> None:
    """能读写内容、管不了成员：内容轴 editor + 治理轴 none。"""
    api.add_space_member(
        ORG,
        SPACE,
        _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        security=SEC_ALICE,
    )
    with pytest.raises(PermissionDeniedError):
        api.add_space_member(
            ORG,
            SPACE,
            _member("carol", SpaceContentRole.VIEWER, SpaceGovernanceRole.NONE),
            security=SEC_BOB,
        )


def test_1_1_a_governance_manager_cannot_read_content(api) -> None:
    """能管成员、看不到内容：治理轴 manager + 内容轴 none。"""
    api.add("alice 偏好深色主题", SPACE_SCOPE, security=SEC_ALICE)
    api.add_space_member(
        ORG,
        SPACE,
        _member("carol", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        security=SEC_ALICE,
    )
    api.add_space_member(
        ORG,
        SPACE,
        _member("dave", SpaceContentRole.VIEWER, SpaceGovernanceRole.NONE),
        security=SEC_CAROL,
    )
    with pytest.raises(PermissionDeniedError):
        api.search("深色主题", SPACE_CONTEXT, security=SEC_CAROL, top_k=5)


def test_governance_ceiling_blocks_appointing_an_owner(api) -> None:
    """治理轴授予上界：管理员可增设管理员，不能设立拥有者。"""
    api.add_space_member(
        ORG,
        SPACE,
        _member("carol", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        security=SEC_ALICE,
    )
    api.add_space_member(
        ORG,
        SPACE,
        _member("dave", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        security=SEC_CAROL,
    )
    with pytest.raises(PermissionDeniedError):
        api.add_space_member(
            ORG,
            SPACE,
            _member("dave", SpaceContentRole.NONE, SpaceGovernanceRole.OWNER),
            security=SEC_CAROL,
        )


def test_removal_ceiling_blocks_removing_a_higher_grade_member(api) -> None:
    """改前值同受上界约束：管理员不能移除拥有者。"""
    api.add_space_member(
        ORG,
        SPACE,
        _member("carol", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        security=SEC_ALICE,
    )
    with pytest.raises(PermissionDeniedError):
        api.remove_space_member(
            ORG, SPACE, Scope(org=ORG, space=SPACE, user="alice"), security=SEC_CAROL
        )


def test_1_5_a_member_record_must_not_raise_the_callers_own_grade(api) -> None:
    """不能给自己提权，且约束覆盖两轴。

    只禁治理轴的话，治理管理员一次加成员即可把自己的内容档设为可编辑——「能管成员而
    看不到内容」那一档一次调用即失效。
    """
    api.add_space_member(
        ORG,
        SPACE,
        _member("carol", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        security=SEC_ALICE,
    )
    with pytest.raises(PermissionDeniedError):
        api.add_space_member(
            ORG,
            SPACE,
            _member("carol", SpaceContentRole.EDITOR, SpaceGovernanceRole.MANAGER),
            security=SEC_CAROL,
        )


def test_1_6_configuring_someone_else_is_not_self_promotion(api) -> None:
    """能给别人配内容档：约束的是自提，不是代他人配置。"""
    api.add_space_member(
        ORG,
        SPACE,
        _member("carol", SpaceContentRole.NONE, SpaceGovernanceRole.MANAGER),
        security=SEC_ALICE,
    )
    api.add_space_member(
        ORG,
        SPACE,
        _member("dave", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        security=SEC_CAROL,
    )
    members = api.list_space_members(ORG, SPACE, security=SEC_ALICE)
    assert any(m.scope.user == "dave" for m in members)


def test_1_4_downgrade_takes_effect_immediately(api) -> None:
    """降权即时生效：移除成员后立即以该成员访问即被拒，不必等缓存过期。

    治理写入不下发事实缓存失效时，被移除的成员在 TTL 内仍按旧快照通过判定，而该窗口
    内的放行既无异常也无审计差异。
    """
    api.add("alice 偏好深色主题", SPACE_SCOPE, security=SEC_ALICE)
    api.add_space_member(
        ORG,
        SPACE,
        _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        security=SEC_ALICE,
    )
    assert api.search("深色主题", SPACE_CONTEXT, security=SEC_BOB, top_k=5).items
    api.remove_space_member(ORG, SPACE, Scope(org=ORG, space=SPACE, user="bob"), security=SEC_ALICE)
    with pytest.raises(PermissionDeniedError):
        api.search("深色主题", SPACE_CONTEXT, security=SEC_BOB, top_k=5)


def test_member_scope_normalisation_matches_the_space_manager(api) -> None:
    """防护侧的成员 scope 归一化与空间管理器的写入侧一致。

    两侧分叉时自提比对的 org 维一空一有值，逐维相同恒不成立——自提禁止整条失效，
    且不报错、不留审计差异。
    """
    api.add_space_member(
        ORG,
        SPACE,
        _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        security=SEC_ALICE,
    )
    stored = [m.scope for m in api.list_space_members(ORG, SPACE, security=SEC_ALICE)]
    guarded = api._normalized_member_scope(SPACE_SCOPE, Scope(user="bob"))
    assert guarded in stored


# -- 检索谓词第一族（F07「检索两族谓词」） --------------------------------- #


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


# -- 空间状态校验（F07「空间状态校验」） ----------------------------------- #


def test_archived_space_allows_reads_and_rejects_writes(api) -> None:
    """归档空间：读动作放行，写动作拒绝，错误类型是参数校验失败而非权限拒绝。"""
    api.add("alice 偏好深色主题", SPACE_SCOPE, security=SEC_ALICE)
    api.archive_space(ORG, SPACE, security=SEC_ALICE)

    assert api.search("深色主题", SPACE_CONTEXT, security=SEC_ALICE, top_k=5) is not None
    with pytest.raises(ValidationError):
        api.add("另一条", SPACE_SCOPE, security=SEC_ALICE)


def test_state_check_runs_after_authorization_so_the_two_errors_stay_distinguishable(
    api,
) -> None:
    """无权调用方得权限拒绝，有权调用方对归档空间得参数校验失败。

    次序若颠倒，无权调用方也能凭错误类型判断出该空间处于归档——与「不泄露空间是否
    存在」的方向相反。该次序被改动不会使其他用例失败，因此由本用例固定。
    """
    api.archive_space(ORG, SPACE, security=SEC_ALICE)
    with pytest.raises(PermissionDeniedError):
        api.add("bob 写入", SPACE_SCOPE, security=SEC_BOB)
    with pytest.raises(ValidationError):
        api.add("alice 写入", SPACE_SCOPE, security=SEC_ALICE)


def test_list_spaces_evaluates_each_candidate_space(api) -> None:
    """``list_spaces`` 逐空间求值，无权的直接剔除、不报错。

    走与单空间入口同一个鉴权方法——分叉即出现「列得出但打不开」或其反向。
    """
    api.create_space(SpaceSpec(org=ORG, space="u-bob", owner=BOB), security=SEC_OPS)
    alice_visible = {info.space for info in api.list_spaces(ORG, security=SEC_ALICE)}
    bob_visible = {info.space for info in api.list_spaces(ORG, security=SEC_BOB)}
    assert alice_visible == {SPACE}
    assert bob_visible == {"u-bob"}


def test_list_spaces_does_not_truncate_candidates_before_authorization(api) -> None:
    """``limit`` 在鉴权之后生效，不截候选（R06 D2）。

    在鉴权之前截断时，可读空间字典序靠后即被挡在候选之外：本用例里 alice 唯一的空间排在
    十二个他人空间之后，``limit=5`` 的候选全是他人空间，过滤后返回空。失效形态是静默空
    返回，与本规约其余各处「截断记 WARNING、通道失败进 errors」的口径相反。
    """
    for index in range(12):
        api.create_space(
            SpaceSpec(org=ORG, space=f"a-other-{index:02d}", owner=BOB),
            security=SEC_OPS,
        )
    # 前提：全库扫描的前五个里没有 alice 的空间，返回条数因而取决于截断次序。
    assert SPACE not in {info.space for info in api._space.list(ORG, limit=5)}
    assert [info.space for info in api.list_spaces(ORG, security=SEC_ALICE, limit=5)] == [SPACE]


def test_list_spaces_applies_limit_after_authorization(api) -> None:
    """``limit`` 是返回条数上限，不是候选条数上限（F07「翻页语义变更」）。"""
    for index in range(4):
        space = f"p-shared-{index}"
        api.create_space(SpaceSpec(org=ORG, space=space, owner=ALICE), security=SEC_OPS)
    assert len(api.list_spaces(ORG, security=SEC_ALICE)) == 5
    assert len(api.list_spaces(ORG, security=SEC_ALICE, limit=2)) == 2


def test_list_spaces_ignores_the_cursor_but_records_it(api) -> None:
    """``cursor`` 标记废弃：候选来自反查索引，全库偏移量无从解释。

    忽略但记进审计明细——静默忽略会让期望翻页的调用方拿到重复页而无从察觉。
    """
    assert [info.space for info in api.list_spaces(ORG, security=SEC_ALICE, cursor="3")] == [SPACE]


def test_list_spaces_lists_a_space_reachable_only_through_an_explicit_grant(api) -> None:
    """靠显式授权取得读权的空间必须列得出（R06 复核 D6）。

    候选若取主体反查索引，这一条必然失败：索引的写入方只有归属登记与成员记录两类，
    ``grant`` 不写索引。失效形态是「直接 ``search`` 读得到、``list_spaces`` 列不出来」，
    且不报错。与 F07 决策 23「不以反查索引粗筛写入候选」是同一条理由，读侧同样成立。
    """
    api.create_space(SpaceSpec(org=ORG, space="p-x", owner=ALICE), security=SEC_OPS)
    api.add_space_member(
        ORG,
        "p-x",
        SpaceMember(
            scope=BOB,
            content_role=SpaceContentRole.EDITOR,
            governance_role=SpaceGovernanceRole.OWNER,
        ),
        security=SEC_ALICE,
    )
    dave = Scope(org=ORG, user="dave")
    api.grant(
        Grant(grantor=Scope(org=ORG, space="p-x"), grantee=dave, actions=[Action.READ]),
        security=SEC_BOB,
    )
    assert "p-x" not in api._membership.spaces_for(dave, ORG)
    listed = api.list_spaces(ORG, security=internal_context(ScopedAuthenticator(dave)))
    assert [info.space for info in listed] == ["p-x"]


def test_list_spaces_rejects_a_non_positive_limit(api) -> None:
    """``limit <= 0`` 在两条路径上都是 ValidationError，不静默返回空。"""
    with pytest.raises(ValidationError):
        api.list_spaces(ORG, security=SEC_ALICE, limit=0)


def test_list_second_stage_strips_the_author_mark(api) -> None:
    """``list`` 第二段不携带作者标记（F07）。

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
    """只能改自己写的：两段鉴权合起来实现该判据。

    第一段的目标是空间、不带条目信息，此时「本人所写」附加集合按可能成立处置，否则
    ``contributor`` 在第一段即被拒；最终边界由第二段的实际作者比对给出。
    """
    api.add_space_member(
        ORG,
        SPACE,
        _member("bob", SpaceContentRole.CONTRIBUTOR, SpaceGovernanceRole.NONE),
        security=SEC_ALICE,
    )
    alice_unit = api.add("alice 写的", SPACE_SCOPE, security=SEC_ALICE)[0]
    bob_unit = api.add("bob 写的", SPACE_SCOPE, security=SEC_BOB)[0]

    api.update(bob_unit.id, SPACE_SCOPE, MemoryPatch(content="bob 改自己的"), security=SEC_BOB)
    with pytest.raises(PermissionDeniedError):
        api.update(
            alice_unit.id,
            SPACE_SCOPE,
            MemoryPatch(content="bob 改别人的"),
            security=SEC_BOB,
        )


# -- 演进模式与任务入口的动作取值（F07「入口到轴与动作的映射」） ------------- #


def test_forget_and_consolidate_are_denied_to_a_contributor(api) -> None:
    """去重与遗忘取 ``UPDATE`` 且不放宽到「本人所写」。

    两种模式改写既有条目、作用对象是整个空间，而可贡献档的 ``UPDATE`` 限本人所写。
    默认动作 ``WRITE`` 若不被覆盖，可贡献档成员即可对他人写入的条目执行遗忘。
    """
    api.add_space_member(
        ORG,
        SPACE,
        _member("bob", SpaceContentRole.CONTRIBUTOR, SpaceGovernanceRole.NONE),
        security=SEC_ALICE,
    )
    api.evolve(SPACE_SCOPE, EvolveMode.EXTRACT, security=SEC_BOB)
    for mode in (EvolveMode.FORGET, EvolveMode.CONSOLIDATE):
        with pytest.raises(PermissionDeniedError):
            api.evolve(SPACE_SCOPE, mode, security=SEC_BOB)


def test_forget_stays_open_to_the_owner(api) -> None:
    """收紧只针对可贡献档：归属主体本人不受影响。"""
    assert api.evolve(SPACE_SCOPE, EvolveMode.FORGET, security=SEC_ALICE)


def test_job_entries_take_the_action_of_the_mode_that_started_the_job(api) -> None:
    """任务状态查询与取消按发起该作业的演进模式取动作。

    取值来自 ``JobInfo.mode``；作业以遗忘模式发起时，查询与取消同样落 ``UPDATE``。
    """
    api.add_space_member(
        ORG,
        SPACE,
        _member("bob", SpaceContentRole.CONTRIBUTOR, SpaceGovernanceRole.NONE),
        security=SEC_ALICE,
    )
    extract_job = api.evolve(SPACE_SCOPE, EvolveMode.EXTRACT, security=SEC_BOB)
    forget_job = api.evolve(SPACE_SCOPE, EvolveMode.FORGET, security=SEC_ALICE)

    api.job_status(extract_job, security=SEC_BOB)
    with pytest.raises(PermissionDeniedError):
        api.job_status(forget_job, security=SEC_BOB)
    with pytest.raises(PermissionDeniedError):
        api.job_cancel(forget_job, security=SEC_BOB)


# -- 建空间与事实缓存 --------------------------------------------------------- #


def test_a_space_read_before_it_exists_does_not_block_the_first_write(api) -> None:
    """建空间下发事实缓存失效：先查后建是常见形态。

    事实缓存对「空间不存在」同样装填一份（元数据与成员皆空）。不清则新空间在一个 TTL 内
    判定无归属、无成员，归属主体本人也写不进去，且无任何错误信号可循。
    """
    with pytest.raises(PermissionDeniedError):
        api.get_space(ORG, "u-new", security=SEC_ALICE)
    api.create_space(SpaceSpec(org=ORG, space="u-new", owner=ALICE), security=SEC_OPS)
    assert api.add("建成即可写", Scope(org=ORG, space="u-new"), security=SEC_ALICE)


# -- 冻结与归档下的可变更范围 ------------------------------------------------- #


def test_a_frozen_space_rejects_changes_other_than_status(api) -> None:
    """冻结态仅放行只改状态的那一次变更。

    判据取 ``SpacePatch`` 的内容而非入口名：``update_space`` 同时能改 ``policy`` 与
    ``principal_path``，二者都是判定依据，只看入口名等于允许在冻结态改写判定依据。
    """
    api.update_space(ORG, SPACE, SpacePatch(status=SpaceStatus.FROZEN), security=SEC_ALICE)
    with pytest.raises(ValidationError):
        api.update_space(ORG, SPACE, SpacePatch(display_name="冻结期改名"), security=SEC_ALICE)
    with pytest.raises(ValidationError):
        api.update_space(
            ORG, SPACE, SpacePatch(policy=SpacePolicy(require_space=True)), security=SEC_ALICE
        )
    info = api.update_space(ORG, SPACE, SpacePatch(status=SpaceStatus.ACTIVE), security=SEC_ALICE)
    assert info.status is SpaceStatus.ACTIVE


# -- 空间策略裁剪（F07「空间策略必须从元数据返回值中裁剪」） ------------------ #


def _with_quota(api, identity):
    api.set_space_policy(
        ORG,
        SPACE,
        SpacePolicy(quotas={"max_units": "10"}),
        security=security_for(api, identity),
    )


def test_policy_is_trimmed_for_a_caller_who_passes_only_the_content_axis(api) -> None:
    """只经内容轴通过则策略置空，经治理轴通过可读策略。

    只把 ``get_space_policy`` 归治理轴而不裁剪 ``get_space`` 的返回值，调用方改调后者
    即可照样读走策略。
    """
    _with_quota(api, ALICE)
    api.add_space_member(
        ORG,
        SPACE,
        _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        security=SEC_ALICE,
    )
    assert api.get_space(ORG, SPACE, security=SEC_ALICE).policy.quotas == {"max_units": "10"}
    assert api.get_space(ORG, SPACE, security=SEC_BOB).policy.quotas == {}
    with pytest.raises(PermissionDeniedError):
        api.get_space_policy(ORG, SPACE, security=SEC_BOB)


def test_list_spaces_trims_the_policy_by_the_same_rule(api) -> None:
    """``list_spaces`` 与 ``get_space`` 同判据同实现。

    分叉即出现「列表里读得到、单查读不到」或其反向。
    """
    _with_quota(api, ALICE)
    api.add_space_member(
        ORG,
        SPACE,
        _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        security=SEC_ALICE,
    )
    listed = {info.space: info for info in api.list_spaces(ORG, security=SEC_BOB)}
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
        security=SEC_ALICE,
    )
    api.add_space_member(
        ORG,
        SPACE,
        _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.NONE),
        security=SEC_ALICE,
    )
    trimmed = api.get_space(ORG, SPACE, security=SEC_BOB)
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
    created = api.grant(grant, security=SEC_ALICE)
    assert api.revoke(created, security=SEC_ALICE) is None


def test_the_passing_axis_is_recorded_in_the_audit_detail(api) -> None:
    """通过的轴落审计：裁剪判据须可追溯到具体一次调用。"""
    api.get_space(ORG, SPACE, security=SEC_ALICE)
    axes = [
        event.detail.get("permission_axis") for event in api._audit.query({"action": "get_space"})
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
    assert api.add("不触发独立点读", SPACE_SCOPE, security=SEC_ALICE)
    api.get_space(ORG, SPACE, security=SEC_ALICE)


def test_space_fact_backend_failure_is_not_disguised_as_permission_deny(api, monkeypatch) -> None:
    """空间事实真源故障原样传播：BackendError（503），不降格为 deny（审核 P2-1）。

    降格的后果是存储故障伪装成越权拒绝——调用方拿着 403 去查权限配置，而问题在
    后端。修复前 ``_read_space_facts`` 把 ``BackendError`` 吞成 ``None``，判定按
    「无归属、无成员」拒绝。
    """
    from jiuwen_memory.common.errors import BackendError

    def _down(*_args, **_kwargs):
        raise BackendError("membership backend down")

    monkeypatch.setattr(api._membership, "facts", _down)

    with pytest.raises(BackendError):
        api.get_space(ORG, SPACE, security=SEC_ALICE)


# -- 第 9 步 代操作委托：空间级生命周期 --------------------------------------- #
#
# 判据本体的单测在 tests/unit/common/security/；这一组测的是空间链上的端到端行为：
# 鉴权点带着 delegation_id 进来，判定宿主回同一 DelegationStore 复核有效期、动作、
# 空间、凭据与会话绑定，再落到第 9 步。
#
# BOT 是**第三方 agent**：不带 user 维，因此不覆盖 alice 的归属登记，成员表里也没有
# 它。这一组的每一条放行都只能来自委托——换成 alice 名下的代理（ALICE_VIA_A1）第 7 步
# 的作者比对就已经通过，用例测不到第 9 步。

BOT = Scope(org=ORG, agent="bot1")

# 覆盖空间级目标的 delegator 只能是纯空间形状：条目真源 scope 归一为空间级、不带主体
# 维，而覆盖判定要求主体主维精确相等。与空间链上 Grant 的 grantor 同一约定
# （见 test_list_spaces_lists_a_space_reachable_only_through_an_explicit_grant）。
_DELEGATION_CONTENT_ACTIONS = frozenset({Action.READ, Action.WRITE, Action.UPDATE, Action.DELETE})


def _delegation(**overrides) -> Delegation:
    """一条覆盖 alice 主空间内容动作的有效委托，按需改单个字段。

    用 ``dataclasses.replace`` 而不是手写 kwargs 合并：字段名写错时立刻报错，不会静默
    产出一条与用例意图不同的委托——一条字段名打错的「过期委托」会变成有效委托，用例
    照样绿。
    """
    base = Delegation(
        delegation_id="d1",
        delegator=SPACE_SCOPE,
        delegate=BOT,
        actions=_DELEGATION_CONTENT_ACTIONS,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return replace(base, **overrides)


def _delegation_store(api):
    """判定实际查询的那一个 DelegationStore。

    经实现层私有真源接缝取，而不是另建一个存储塞进去：不变量 27 要求实际判定访问
    同一具名实例，测试若自带存储就测不出接错真源（症状是生产里委托恒不命中，而
    测试全绿）。
    """
    stores = _delegation_sources_for(api._authorizer)
    assert len(stores) == 1
    return stores[0]


def _bot_security(*, delegation_id: str = "d1", credential_id: str = "", session: str = ""):
    """声明了委托的 BOT 请求上下文。

    必须经 ``internal_context`` 这个受控入口构造：``delegation_id`` 与 ``credential_id``
    都进了 ``_bind_origin`` 的 HMAC，在调用点用 ``dataclasses.replace`` 往上下文里塞
    委托声明会被鉴权点判成伪造。
    """
    actor = replace(BOT, session=session) if session else BOT
    return internal_context(
        ScopedAuthenticator(actor, delegation_id=delegation_id, credential_id=credential_id)
    )


def test_a_third_party_agent_reaches_nothing_without_a_delegation(api) -> None:
    """基线：不带委托的第三方 agent 读写皆拒。

    这一条固定的是「后面每一条放行都来自委托」——它若先失败，整组用例的结论都不成立。
    """
    api.add("alice 偏好深色主题", SPACE_SCOPE, security=SEC_ALICE)
    with pytest.raises(PermissionDeniedError):
        api.add("bot 写的", SPACE_SCOPE, security=_bot_security())
    with pytest.raises(PermissionDeniedError):
        api.search("深色主题", SPACE_CONTEXT, security=_bot_security(), top_k=5)


def test_a_valid_delegation_lets_the_agent_act_on_content(api) -> None:
    """有效委托放行内容轴条目动作：写得进、读得到。

    第 9 步的放行分支若不可达（例如委托方一侧的判据要求 delegator 带 user 维，与
    「delegator 须覆盖空间级目标」互斥），本用例是唯一会失败的一条——合法代操作在
    启用空间隔离后静默失效，不报错、不落审计异常。
    """
    _delegation_store(api).add(_delegation())
    assert api.add("bot 代 alice 写的", SPACE_SCOPE, security=_bot_security())
    assert api.search("代 alice", SPACE_CONTEXT, security=_bot_security(), top_k=5).items


def test_declared_delegation_is_decided_before_grant_store_access(api, monkeypatch) -> None:
    """有效委托必须在 Grant 前终局，不得先触达与结论无关的 GrantStore。"""
    _delegation_store(api).add(_delegation())
    grant_store = api._authorizer.management_grant_stores()[0]

    def _unexpected_grant_lookup(*_args, **_kwargs):
        raise AssertionError("GrantStore must not be queried before a declared delegation")

    monkeypatch.setattr(grant_store, "find_active", _unexpected_grant_lookup)
    assert api.add("委托先于授权", SPACE_SCOPE, security=_bot_security())


def test_a_forged_delegation_id_is_rejected(api) -> None:
    """伪造的委托标识拒绝：真源里没有这条记录。

    「不存在」与「已撤销/已过期」共用同一个拒绝原因：区分它们等于给出一条委托标识的
    枚举侧信道。
    """
    _delegation_store(api).add(_delegation())
    with pytest.raises(PermissionDeniedError):
        api.search(
            "代 alice",
            SPACE_CONTEXT,
            security=_bot_security(delegation_id="d-forged"),
            top_k=5,
        )


def test_an_expired_delegation_is_rejected(api) -> None:
    """过期委托拒绝。

    ``Delegation.expires_at`` 没有「None = 永久」形态，有效期是必填项；这一条固定的是
    有效期**每次判定都复核**，而不是只在写入时校验一次。
    """
    _delegation_store(api).add(
        _delegation(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    with pytest.raises(PermissionDeniedError):
        api.add("bot 代 alice 写的", SPACE_SCOPE, security=_bot_security())


def test_a_revoked_delegation_is_rejected_and_cannot_be_replayed(api) -> None:
    """撤销即刻生效，且同 id 重放不复活（不变量 27）。

    撤销单调是撤销这件事本身的意义：若同 ``delegation_id`` 的写入能覆盖已撤销的记录，
    任何持有原委托内容的一方都能把它写回来，撤销就只是一次延迟。
    """
    store = _delegation_store(api)
    store.add(_delegation())
    assert api.add("撤销前", SPACE_SCOPE, security=_bot_security())

    store.revoke("d1")
    with pytest.raises(PermissionDeniedError):
        api.add("撤销后", SPACE_SCOPE, security=_bot_security())

    store.add(_delegation())  # 同 id 重放
    with pytest.raises(PermissionDeniedError):
        api.add("重放后", SPACE_SCOPE, security=_bot_security())


def _last_deny_rule(api) -> str:
    """最近一条拒绝审计的判据名。

    绑定类用例断言的是**哪一条**判据拒绝的，不只是「拒了」：每条绑定都有各自的失效
    形态（守卫漏写、比较取反、比错字段），而它们的现象都是「拒绝」。只断言拒绝的用例
    在守卫漏写时照样绿——另一条判据顺手拒了它。
    """
    denies = [event for event in api._audit.query({}) if event.decision == "deny"]
    assert denies, "本次调用应落一条 deny 审计"
    return str(denies[-1].detail.get("permission_rule", ""))


def test_a_delegation_confined_to_other_spaces_does_not_reach_this_one(api) -> None:
    """``allowed_spaces`` 是空间级越界闸门：不含本空间即拒绝。

    正反两侧都测：只测拒绝一侧的用例在「守卫把集合判反」时仍然绿——它会让恰好**不在**
    清单里的空间成为唯一可达的空间。
    """
    store = _delegation_store(api)
    store.add(_delegation(allowed_spaces=frozenset({"u-someone-else"})))
    with pytest.raises(PermissionDeniedError):
        api.add("越界", SPACE_SCOPE, security=_bot_security())
    assert _last_deny_rule(api) == "delegation_binding"

    store.add(_delegation(allowed_spaces=frozenset({SPACE, "u-someone-else"})))
    assert api.add("清单内", SPACE_SCOPE, security=_bot_security())


def test_a_credential_bound_delegation_only_works_with_that_credential(api) -> None:
    """绑定凭据后换一把 key 的同一个 agent 用不了这条委托。

    这条绑定把凭据泄露的爆炸半径收敛在单把 key 上；缺它则任何持有该 agent 任一凭据的
    一方都能用这条委托。
    """
    store = _delegation_store(api)
    store.add(_delegation(bound_credential_id="k1"))
    with pytest.raises(PermissionDeniedError):
        api.add("换了 key", SPACE_SCOPE, security=_bot_security(credential_id="k2"))
    assert _last_deny_rule(api) == "delegation_binding"

    assert api.add("原 key", SPACE_SCOPE, security=_bot_security(credential_id="k1"))


def test_a_session_bound_delegation_only_works_in_that_session(api) -> None:
    """绑定会话后，同一 agent 换一个会话用不了这条委托。

    会话维不参与覆盖判定（判定按主体两维比对），这条绑定是它在授权上唯一的落点——
    漏写则一条为单次会话签发的委托变成该 agent 的长期权限。
    """
    store = _delegation_store(api)
    store.add(_delegation(bound_session="s1"))
    with pytest.raises(PermissionDeniedError):
        api.add("换了会话", SPACE_SCOPE, security=_bot_security(session="s2"))
    assert _last_deny_rule(api) == "delegation_binding"

    assert api.add("原会话", SPACE_SCOPE, security=_bot_security(session="s1"))


def test_an_action_outside_the_allowlist_is_rejected(api) -> None:
    """动作不在 allowlist 内即拒绝：一条只读委托写不进东西。

    ``permits`` 同时查 allowlist 与 ``DELEGATABLE_ACTIONS``，两个条件缺一不可——只查
    allowlist 会让一条写坏或被篡改的委托记录直接拿到管理动作。
    """
    store = _delegation_store(api)
    store.add(_delegation())
    assert api.add("bot 自己写的", SPACE_SCOPE, security=_bot_security())

    store.add(_delegation(actions=frozenset({Action.READ})))
    # 读侧照常：证明拒绝来自动作 allowlist，而不是这条委托整体失效了。
    assert api.search("自己写的", SPACE_CONTEXT, security=_bot_security(), top_k=5).items
    with pytest.raises(PermissionDeniedError):
        api.add("只读委托写不进", SPACE_SCOPE, security=_bot_security())
    assert _last_deny_rule(api) == "delegation_action"


def test_another_agent_cannot_borrow_a_delegation_id(api) -> None:
    """委托标识不是凭据：另一个 agent 拿着它用不了。

    委托标识会经上下文在服务间流转，只要它单独可用，泄露一次就等于把被委托方的权限
    转给了任何看得到它的一方。
    """
    _delegation_store(api).add(_delegation())
    other_bot = internal_context(
        ScopedAuthenticator(Scope(org=ORG, agent="bot2"), delegation_id="d1")
    )
    with pytest.raises(PermissionDeniedError):
        api.add("借用别人的委托", SPACE_SCOPE, security=other_bot)
    assert _last_deny_rule(api) == "delegation_binding"


def test_an_agent_cannot_re_delegate_to_another_agent(api) -> None:
    """委托方一侧必须是非 agent 主体：agent 再委托 agent 不成立。

    委托关系一旦能自我复制，撤销就追不上——撤销一条委托时无从知道它派生出了多少条。
    判据是「委托方不带 agent 维」，与「被委托方必须带 agent 维」配对。
    """
    store = _delegation_store(api)
    store.add(_delegation(delegator=Scope(org=ORG, space=SPACE, agent="bot9")))
    with pytest.raises(PermissionDeniedError):
        api.add("agent 转委托", SPACE_SCOPE, security=_bot_security())
    assert _last_deny_rule(api) == "delegation_principal"

    # 委托方留空同样不成立：一条没写清「委托方是谁」的记录不算成立的委托。
    store.add(_delegation(delegation_id="d2", delegator=Scope()))
    with pytest.raises(PermissionDeniedError):
        api.add("委托方留空", SPACE_SCOPE, security=_bot_security(delegation_id="d2"))
    assert _last_deny_rule(api) == "delegation_principal"


def test_a_delegation_to_a_non_agent_principal_is_rejected(api) -> None:
    """被委托方必须是 agent/service 这类非人主体。

    委托是「机器代人操作」这一件事的表达；被委托方是自然人时该用的是 Grant，两者的
    撤销与审计形态不同，混用会让「谁在代谁操作」在审计里无从还原。
    """
    _delegation_store(api).add(_delegation(delegate=BOB))
    with pytest.raises(PermissionDeniedError):
        api.add("委托给自然人", SPACE_SCOPE, security=_bot_security())
    assert _last_deny_rule(api) == "delegation_principal"


def test_a_failed_delegation_does_not_fall_back_to_an_explicit_grant(api) -> None:
    """声明了委托就不再回落 Grant：失效委托的拒绝不被另一条规则掩盖。

    两次调用只差「有没有声明委托」这一项，Grant 全程有效：
    - 不声明委托 → 走 Grant，通过；
    - 声明一条已撤销的委托 → 拒绝，且拒绝原因指向委托查找，不是 Grant。

    回落的后果不是越权而是**审计失真**：调用方明说「我在代操作」，代操作凭据已经失效，
    而请求照常通过、审计里记的是 Grant 命中——委托失效过这件事在事后无从还原。
    """
    # grant 不在归属主体档的两级清单内，归属主体本人也需要成员记录才授得出去；
    # 因此先由 alice 指派一位治理 OWNER，再由他授出（同
    # test_list_spaces_lists_a_space_reachable_only_through_an_explicit_grant）。
    api.add_space_member(
        ORG,
        SPACE,
        _member("bob", SpaceContentRole.EDITOR, SpaceGovernanceRole.OWNER),
        security=SEC_ALICE,
    )
    api.grant(
        Grant(grantor=SPACE_SCOPE, grantee=BOT, actions=[Action.READ]),
        security=SEC_BOB,
    )
    # Grant 是活的：不声明委托时同一调用通过（行级可见范围由第一族谓词另行收窄，
    # 这里要的是「没被拒」）。
    api.search("任意", SPACE_CONTEXT, security=internal_context(ScopedAuthenticator(BOT)), top_k=5)

    store = _delegation_store(api)
    store.add(_delegation())
    store.revoke("d1")
    with pytest.raises(PermissionDeniedError):
        api.search("任意", SPACE_CONTEXT, security=_bot_security(), top_k=5)
    assert _last_deny_rule(api) == "delegation_lookup"


def test_a_content_delegation_does_not_reach_governance_or_whole_space_export(api) -> None:
    """委托不得越到治理轴与「逐维相同」入口（第 7 步两条排除同样约束第 9 步）。

    委托放行的是「代理替人操作」，与第 7 步归属对比同一类情形，适用范围必须相同——
    否则一条带 UPDATE/DELETE 的内容委托能改策略、删空间，一条带 READ 的能整空间导出，
    而这三件事对归属主体**本人的代理**都是禁止的（见 test_2_5 与 test_2_7）。

    断言判据名不以 ``delegation`` 开头：这一步在这些入口上**整体不参与**，而不是
    「参与了、恰好没通过」。后者会随委托内容变化而翻转。
    """
    _delegation_store(api).add(_delegation())  # READ/WRITE/UPDATE/DELETE 全给
    for call in (
        lambda: api.export_space(ORG, SPACE, security=_bot_security()),
        lambda: api.update_space(
            ORG, SPACE, SpacePatch(display_name="X"), security=_bot_security()
        ),
        lambda: api.delete_space(ORG, SPACE, security=_bot_security()),
    ):
        with pytest.raises(PermissionDeniedError):
            call()
        assert not _last_deny_rule(api).startswith("delegation")


def test_a_delegation_opens_the_space_gate_but_not_the_row_boundary(api) -> None:
    """委托放开的是空间级闸门，不放开行级可见范围。

    第一族谓词按调用形态收窄：自主运行的 agent 只看得见自己写的条目（F07「检索两族
    谓词」）。两层因此是独立的——委托让 agent 进得了这个空间，进来之后能看见哪些条目
    仍由谓词决定，委托内容再宽也不放宽这一层。

    这一层的失效形态是**静默的**：谓词漏注入时 agent 照常返回结果，只是多了别人的
    条目，调用方无从察觉。因此这条边界要正面测出来，而不是从「委托测试都绿」推断。

    同时它解释了本组其余用例为何多以 ``add`` 作探针：读侧被这层谓词过滤后，「拒绝」
    与「通过但没有可见行」在返回值上都是空结果，用读侧当探针分不出这两件事。
    """
    _delegation_store(api).add(_delegation())
    api.add("alice 写的部署笔记", SPACE_SCOPE, security=SEC_ALICE)
    api.add("bot 写的部署笔记", SPACE_SCOPE, security=_bot_security())

    seen = api.search("部署笔记", SPACE_CONTEXT, security=_bot_security(), top_k=5)
    assert {item.content for item in seen.items} == {"bot 写的部署笔记"}

    # 归属主体本人不受这条谓词收窄：同一空间内两条都在。
    by_owner = api.search("部署笔记", SPACE_CONTEXT, security=SEC_ALICE, top_k=5)
    assert {item.content for item in by_owner.items} == {
        "alice 写的部署笔记",
        "bot 写的部署笔记",
    }
