"""归属判定与多空间读写的端到端行为（S09「多空间读写」，F03 分能力用例）。

判定表与两个落盘不变量的单测在 ``tests/unit/construction/test_router_table.py``；本文件测
的是接线：写入侧的候选空间集合与落点、检索侧的两族谓词与跨空间合并、按谓词的批量删除。

判定实现取一个按关键词作答的桩：模型实现的输出不可复现，而本文件断言的是接线，不是模型
判得准不准。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.common.errors import NotFoundError, PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.space_roles import SpaceContentRole, SpaceGovernanceRole
from jiuwen_memory.common.type_def import (
    Context,
    FilterClause,
    FilterExpr,
    FilterOp,
    MemoryUnit,
    RecallChannel,
    Scope,
    iter_clauses,
)
from jiuwen_memory.config import Config
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.router import (
    Router,
    RouterProducer,
    RouteTable,
    build_decision,
    parse_route_table,
)
from jiuwen_memory.control import SpaceMember, SpaceSpec, SpaceStatus
from jiuwen_memory.control.types import (
    BatchWriteItem,
    DeleteMode,
    DeleteSelector,
    MemoryPatch,
)

pytestmark = pytest.mark.unit

ORG = "acme"
OPS = Scope()
ALICE = Scope(org=ORG, user="alice")
ALICE_VIA_A1 = Scope(org=ORG, user="alice", agent="a1")
BOB = Scope(org=ORG, user="bob")

ALICE_SPACE = "u_alice"
BOB_SPACE = "u_bob"
PROJECT_SPACE = "p_p1"

ROUTE_TABLE = {
    "coord_entities": ["project", "team"],
    "memory_classes": [
        {
            "name": "user_memory",
            "owner": "user",
            "space_template": "u_{user}",
            "fallback": True,
            "description": "facts about the user",
        },
        {
            "name": "project_memory",
            "owner": "project",
            "space_template": "p_{project}",
            "cross_user": True,
            "members": "project participants",
        },
        {"name": "team_memory", "owner": "team", "record_only": True},
    ],
    "narrow_dims": [
        {"entity": "agent", "tag_key": "agent_id"},
        {"entity": "session", "tag_key": "session_id"},
        {"entity": "project", "tag_key": "project_id"},
    ],
}


class _KeywordRouter(Router):
    """按关键词作答的判定桩：含「项目」判 project、含「团队」判 team、其余判 user。

    收窄维一律判真——本文件要断言的是标签取值从坐标来、键恒存在，判真与判假的分支在
    判定表单测里覆盖。
    """

    def __init__(self, table: RouteTable) -> None:
        self._table = table
        # 每次 route 收到的单元 id，供「探针 id 逐条唯一」一条断言。
        self.seen_ids: list[list[str]] = []

    @property
    def table(self) -> RouteTable:
        return self._table

    def operator_type(self) -> OperatorType:
        return OperatorType.ROUTER

    def health(self) -> None:
        return None

    def route(self, units, ctx):
        self.seen_ids.append([unit.id for unit in units])
        decisions = []
        for unit in units:
            if "项目" in unit.content:
                name = "project_memory"
            elif "团队" in unit.content:
                name = "team_memory"
            else:
                name = "user_memory"
            hits = tuple(dim.tag_key for dim in ctx.narrow_dims)
            decisions.append(build_decision(unit, name, hits, ctx))
        return decisions


@RouterProducer.register("keyword_stub")
def _build_stub(config):
    return _KeywordRouter(
        parse_route_table(
            {
                "coord_entities": config.get("coord_entities"),
                "memory_classes": config.get("memory_classes"),
                "narrow_dims": config.get("narrow_dims"),
            }
        )
    )


def _kernel(*, with_router: bool = True, policies: dict[str, str] | None = None):
    """cloud 引擎 + 空间感知判定（+ 归属判定）。

    in_memory 引擎只支持 ``scope.space == ""``，测不了空间级落点。
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
    config = {
        "engine": {"default": {"target": "cloud", "params": engine_params}},
        "permission": {"default": {"target": "space_aware", "params": {"db_path": ":memory:"}}},
    }
    if with_router:
        config["router"] = {"default": {"target": "keyword_stub", "params": dict(ROUTE_TABLE)}}
    return build_kernel(policies=policies, config=Config.from_dict(config))


def _member(user: str, content: SpaceContentRole) -> SpaceMember:
    return SpaceMember(
        scope=Scope(org=ORG, user=user),
        content_role=content,
        governance_role=SpaceGovernanceRole.NONE,
    )


def _open_project_space(api, *, with_bob: bool = True) -> None:
    """开通协作空间：alice 作项目负责人建空间，逐参与者写一条成员记录。

    过渡期无组织级角色，替他人建空间与加成员的管理服务身份无从模拟，因此由负责人建
    空间并加成员。成员表非空即共享形态成立，这是本文件断言的前提。
    """
    api.create_space(SpaceSpec(org=ORG, space=PROJECT_SPACE, owner=ALICE), identity=OPS)
    if with_bob:
        api.add_space_member(
            ORG, PROJECT_SPACE, _member("bob", SpaceContentRole.EDITOR), identity=ALICE
        )


@pytest.fixture
def api():
    kernel = _kernel()
    api = kernel.api
    api.create_space(SpaceSpec(org=ORG, space=ALICE_SPACE, owner=ALICE), identity=OPS)
    api.create_space(SpaceSpec(org=ORG, space=BOB_SPACE, owner=BOB), identity=OPS)
    _open_project_space(api)
    return api


def _units_in(api, space: str, identity: Scope) -> list[MemoryUnit]:
    return api.list(Scope(org=ORG, space=space), identity=identity, limit=100).items


def test_the_api_layer_and_the_construction_layer_share_one_router(api) -> None:
    """两层取的是同一个具名实例，因而共用一份判定表。

    各建一份的后果是写入边界拒绝的键集合与判定实际写入的键集合可以不一致——判定自己写的
    标签会在下一次写入时被自己的边界校验拒绝。
    """
    assert api._router is not None
    assert api._engine._evolver._router is api._router
    assert api._route_table.tag_keys == {"agent_id", "session_id", "project_id", "team_id"}


# -- 写入时自动选空间 ------------------------------------------------------ #


def test_a_given_scope_is_not_routed(api) -> None:
    """传了 scope 就是它，判定不介入——两条路径互斥、不叠加。"""
    api.add("项目部署在集群 A", Scope(org=ORG, space=ALICE_SPACE), identity=ALICE)
    assert len(_units_in(api, ALICE_SPACE, ALICE)) == 1
    assert not _units_in(api, PROJECT_SPACE, ALICE)


def test_an_omitted_scope_is_routed_into_the_project_space(api) -> None:
    """省略 scope 即按归属坐标判落点：项目事实落协作空间，不落个人主空间。"""
    api.add("项目部署在集群 A", identity=ALICE, coords={"project": "p1"})
    landed = _units_in(api, PROJECT_SPACE, ALICE)
    assert [unit.content for unit in landed] == ["项目部署在集群 A"]
    assert landed[0].system_metadata["memory_class"] == "project_memory"
    assert not _units_in(api, ALICE_SPACE, ALICE)


def test_a_user_fact_lands_in_the_individual_space(api) -> None:
    api.add("偏好深色主题", identity=ALICE, coords={"project": "p1"})
    landed = _units_in(api, ALICE_SPACE, ALICE)
    assert [unit.system_metadata["memory_class"] for unit in landed] == ["user_memory"]


def test_a_record_only_class_lands_in_fallback_and_keeps_its_tag(api) -> None:
    """记录维类别不落独立空间：落 fallback，实体记成标签。"""
    api.add("团队用同一套评审流程", identity=ALICE, coords={"team": "t1"})
    landed = _units_in(api, ALICE_SPACE, ALICE)
    assert landed[0].system_metadata["memory_class"] == "team_memory"
    assert landed[0].system_metadata["team_id"] == "t1"


def test_all_routing_tag_keys_are_written_even_when_the_coordinate_is_absent(api) -> None:
    """键恒存在：坐标里没有 project 时该键仍写空串，否则集合谓词把条目静默筛掉。"""
    api.add("偏好深色主题", identity=ALICE)
    landed = _units_in(api, ALICE_SPACE, ALICE)[0]
    assert landed.system_metadata["project_id"] == ""
    assert landed.system_metadata["team_id"] == ""
    assert landed.system_metadata["session_id"] == ""


def test_routing_cannot_reach_a_space_the_caller_cannot_write(api) -> None:
    """判定不得扩权：坐标指向的空间无写权时它不进候选集，内容落 fallback。

    bob 不是 p1 的参与者时把坐标填成 p1，仍只能落自己的主空间。
    """
    api.remove_space_member(ORG, PROJECT_SPACE, Scope(org=ORG, user="bob"), identity=ALICE)
    api.add("项目部署在集群 A", identity=BOB, coords={"project": "p1"})
    assert not _units_in(api, PROJECT_SPACE, ALICE)
    assert len(_units_in(api, BOB_SPACE, BOB)) == 1


def test_the_fallback_space_is_created_on_first_write(api) -> None:
    """fallback 空间不存在即按调用方身份自动创建并登记归属（默认开）。

    空间名由调用方自己的身份渲染而来，别的主体渲染不出它，归属该登记给谁是确定的。
    """
    fresh = _kernel().api  # 一个空间都没建
    units = fresh.add("偏好深色主题", identity=ALICE, coords={"project": "p1"})
    assert units[0].scope.space == ALICE_SPACE
    info = fresh.space_manager.get(ORG, ALICE_SPACE)
    assert [owner.user for owner in info.owners] == ["alice"]
    # 只建 fallback：坐标指向的协作空间不自动建——它的成员表内核产生不了。
    with pytest.raises(NotFoundError):
        fresh.space_manager.get(ORG, PROJECT_SPACE)


def test_auto_create_is_switchable_off() -> None:
    """关掉开关即回到硬前置：接入方须在用户开通时预建主空间。"""
    kernel = build_kernel(
        config=Config.from_dict(
            {
                "engine": {
                    "default": {
                        "target": "cloud",
                        "params": {
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
                        },
                    }
                },
                "permission": {
                    "default": {"target": "space_aware", "params": {"db_path": ":memory:"}}
                },
                "router": {"default": {"target": "keyword_stub", "params": dict(ROUTE_TABLE)}},
            }
        ),
        policies={"space.auto_create_fallback": "false"},
    )
    api = kernel.api
    # 协作空间可写，但主空间没建：仍整体拒绝，不把内容落进协作空间——判不准时无处可落，
    # 静默落到别处等于把兜底落点交给判定实现决定。
    _open_project_space(api, with_bob=False)
    with pytest.raises(PermissionDeniedError, match="nowhere to fall back"):
        api.add("项目部署在集群 A", identity=ALICE, coords={"project": "p1"})
    assert not api.list(
        Scope(org=ORG, space=PROJECT_SPACE), identity=ALICE, limit=10
    ).items


def test_auto_create_does_not_resurrect_a_space_the_caller_cannot_write(api) -> None:
    """自动创建只处理「还没开通」，不处理「不让你写」。

    归档自己的主空间后再写入仍被拒，空间状态不被覆盖——否则任何一次写入都能把归档
    撤销，归档这条约束形同虚设。
    """
    api.archive_space(ORG, ALICE_SPACE, identity=ALICE)
    with pytest.raises(ValidationError, match="not writable"):
        api.add("偏好深色主题", identity=ALICE, coords={"project": "p1"})
    assert api.space_manager.get(ORG, ALICE_SPACE).status is SpaceStatus.ARCHIVED


def test_an_omitted_scope_without_a_routing_table_is_a_missing_argument() -> None:
    """不启用判定时行为不变：省略 scope 就是缺参，判定路径不可达。"""
    api = _kernel(with_router=False).api
    api.create_space(SpaceSpec(org=ORG, space=ALICE_SPACE, owner=ALICE), identity=OPS)
    with pytest.raises(ValidationError, match="scope is required"):
        api.add("偏好深色主题", identity=ALICE)


def test_a_routing_table_without_space_authorization_is_rejected_at_assembly() -> None:
    """判定表已配置而判定实现不读空间事实，装配期即拒绝（R06 第二章「四种组合」）。

    该组合把内容按坐标分流进协作空间，而协作空间没有任何权限边界，等于把内容写进一个
    组织内任意主体可读的位置。方向为放行，且从调用侧看不出异常——写入成功、检索也拿得到，
    只是拿得到的人多了。
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
    config = {
        "engine": {"default": {"target": "cloud", "params": engine_params}},
        "router": {"default": {"target": "keyword_stub", "params": dict(ROUTE_TABLE)}},
    }
    with pytest.raises(ValidationError, match="space facts"):
        build_kernel(config=Config.from_dict(config))


def test_space_authorization_without_a_routing_table_assembles() -> None:
    """反方向是合法部署：空间之间有权限边界，``scope`` 仍必填。"""
    assert _kernel(with_router=False).api is not None


def test_batch_items_without_a_scope_are_routed_per_item(api) -> None:
    """判定按写入路径生效、不按单条与批量入口区分。

    缺这一条时批量入口的省略 scope 停在「缺参」上，``coords`` 加在它上面没有实际作用。
    """
    from jiuwen_memory.control import BatchWriteItem

    result = api.batch_add(
        [BatchWriteItem(content="项目部署在集群 A"), BatchWriteItem(content="偏好深色主题")],
        identity=ALICE,
        coords={"project": "p1"},
    )
    assert [outcome.error for outcome in result.outcomes] == ["", ""]
    assert len(_units_in(api, PROJECT_SPACE, ALICE)) == 1
    assert len(_units_in(api, ALICE_SPACE, ALICE)) == 1


def test_routing_probes_carry_unique_ids(api) -> None:
    """一批多条时探针 id 逐条唯一。

    ``LLMRouter`` 按 id 把模型给的结论对回条目。``MemoryUnit.id`` 缺省是空串，探针不显式
    赋值时一批内各条的 id 全相同，比对表被逐条覆盖，整批取到最后一条的结论——失效方向是
    静默错落点，可扩权（个人内容进协作空间）也可失权，而落点仍在候选集内，
    ``route_batch`` 的候选集校验兜不住。
    """
    from jiuwen_memory.control import BatchWriteItem

    api.batch_add(
        [BatchWriteItem(content="项目部署在集群 A"), BatchWriteItem(content="偏好深色主题")],
        identity=ALICE,
        coords={"project": "p1"},
    )
    batches = [ids for ids in api._router.seen_ids if len(ids) > 1]
    assert batches, "批量入口应把省略 scope 的两条一次送判"
    ids = batches[-1]
    assert all(ids), "探针 id 不得为空"
    assert len(set(ids)) == len(ids), "同批探针 id 不得重复"


def test_the_derived_units_of_an_infer_write_are_routed_in_the_construction_layer(api) -> None:
    """同步抽取路径：判定在构建层逐条进行，上下文经源单元的瞬态键传下去。

    这条路径与单条判定入口分工不同——它判的是抽取产出的派生单元，而不是调用方给的原文。
    """
    units = api.add(
        "项目部署在集群 A",
        identity=ALICE,
        coords={"project": "p1"},
        system_metadata={"infer": "true"},
    )
    assert [unit.scope.space for unit in units] == [PROJECT_SPACE]
    assert [unit.system_metadata["memory_class"] for unit in units] == ["project_memory"]
    landed = _units_in(api, PROJECT_SPACE, ALICE)
    assert [unit.content for unit in landed] == [unit.content for unit in units]


def test_the_routing_context_reaches_neither_the_returned_units_nor_the_store(api) -> None:
    """判定上下文用完即弃：既不落盘，也不出现在回传给调用方的对象上。

    该键在判定应用处（``apply_decisions``）从 ``system_metadata`` 剥除，落盘与回传对象
    共用这一处：漏掉即调用方拿到一个内部对象，且它会被序列化进真源。
    """
    units = api.add(
        "项目部署在集群 A",
        identity=ALICE,
        coords={"project": "p1"},
        system_metadata={"infer": "true"},
    )
    assert all("route_ctx" not in unit.system_metadata for unit in units)
    assert all(
        "route_ctx" not in unit.system_metadata for unit in _units_in(api, PROJECT_SPACE, ALICE)
    )


def test_the_routing_context_does_not_reach_the_write_permission_context(api) -> None:
    """判定上下文也不进写入鉴权上下文（R06 D3）。

    ``infer`` / ``procedural`` 路径把 :class:`RouteContext` 对象经瞬态键放进
    ``system_metadata`` 交给构建层，而写入鉴权上下文对每个取值取 ``str()``——不剥除即把
    上千字符的对象字面量当成判据传给判定实现。检索侧的兄弟函数一直有对应处理。
    """
    seen: list[dict[str, str]] = []
    original = api._perm.decide

    def _capture(identity, target, action, *, context=None, **kwargs):
        if context is not None and context.resource_type == "write_input":
            seen.append(dict(context.metadata))
        return original(identity, target, action, context=context, **kwargs)

    api._perm.decide = _capture
    api.add(
        "项目部署在集群 A",
        identity=ALICE,
        coords={"project": "p1"},
        system_metadata={"infer": "true"},
    )
    assert seen, "写入鉴权上下文未被构造，用例前提不成立"
    assert all("route_ctx" not in metadata for metadata in seen)


def test_the_returned_units_of_a_routed_infer_write_are_readable_by_id(api) -> None:
    """落盘产物按回传对象取，不按入参 scope 回读——判定改了 scope，按原 scope 读会落空。"""
    units = api.add(
        "项目部署在集群 A",
        identity=ALICE,
        coords={"project": "p1"},
        system_metadata={"infer": "true"},
    )
    fetched = api.get(units[0].id, units[0].scope, identity=ALICE)
    assert fetched.id == units[0].id


# -- 写入边界校验 ---------------------------------------------------------- #


def test_the_caller_cannot_assign_a_kernel_coordinate(api) -> None:
    """内核三项坐标取自调用方身份，赋值即拒绝。

    静默丢弃的话调用方以为自己指定了归属、实际被忽略，而落点与检索结果都会与预期不符且
    没有提示。判定表的加载期第 12 条已禁止配置声明这三项，调用层跟着拒绝才是同一口径。
    """
    with pytest.raises(ValidationError, match="不得给内核坐标赋值"):
        api.add("偏好深色主题", identity=ALICE, coords={"user": "bob"})
    with pytest.raises(ValidationError, match="不得给内核坐标赋值"):
        api.search(
            "深色主题",
            Context(scope=Scope(org=ORG, space=ALICE_SPACE)),
            identity=ALICE,
            coords={"session": "s9"},
        )


def test_the_caller_cannot_assign_a_routing_tag_key(api) -> None:
    """判定标签键参与检索过滤，调用方能自行赋值即可绕过收窄。"""
    with pytest.raises(ValidationError, match="判定标签 key"):
        api.add(
            "偏好深色主题",
            Scope(org=ORG, space=ALICE_SPACE),
            identity=ALICE,
            system_metadata={"session_id": "别人的会话"},
        )


def test_the_caller_cannot_declare_a_foreign_principal_on_the_write_scope(api) -> None:
    """归属不由调用方声明：入参 scope 的主体维与身份不一致即拒绝。"""
    with pytest.raises(ValidationError, match="does not match the caller identity"):
        api.add(
            "偏好深色主题",
            Scope(org=ORG, space=ALICE_SPACE, user="bob"),
            identity=ALICE,
        )


def test_the_landed_scope_keeps_only_org_and_space(api) -> None:
    """条目落盘 scope 归一为两维：主体维留在键上会使判定按各维相等放行。"""
    api.add("偏好深色主题", Scope(org=ORG, space=ALICE_SPACE, session="s1"), identity=ALICE)
    landed = _units_in(api, ALICE_SPACE, ALICE)[0]
    assert landed.scope == Scope(org=ORG, space=ALICE_SPACE)


# -- 跨空间检索 ------------------------------------------------------------ #


def test_the_fanout_limit_is_configurable_and_bounds_both_sides() -> None:
    """一次调用参与的空间数上限取自策略 ``space.fanout_limit``，写入侧与检索侧同值。

    这是功能天花板而非性能参数：主体同时参与的空间超过该值时，超出部分写入不进候选、
    检索也取不到。够用与否取决于接入方的协作规模假设，因此可配置而不写死在内核里。
    """
    api = _kernel(policies={"space.fanout_limit": "1"}).api
    api.create_space(SpaceSpec(org=ORG, space=ALICE_SPACE, owner=ALICE), identity=OPS)
    _open_project_space(api, with_bob=False)

    # 写入侧：候选被截到只剩 fallback，项目内容也落 fallback。
    api.add("项目部署在集群 A", identity=ALICE, coords={"project": "p1"})
    assert len(_units_in(api, ALICE_SPACE, ALICE)) == 1
    assert _units_in(api, PROJECT_SPACE, ALICE) == []

    # 检索侧：候选同样截到 1 个，另一个可读空间不参与。
    assert api._membership.spaces_for(ALICE, ORG) == (PROJECT_SPACE, ALICE_SPACE)
    assert api._search_candidates(ALICE, ORG, None) == [PROJECT_SPACE]


def test_an_unusable_fanout_limit_falls_back_to_the_default() -> None:
    """配置项写错时按缺省值跑并记 WARNING：本项是规模上限而非判据，不值得让部署起不来。"""
    for raw in ("0", "-1", "eight"):
        api = _kernel(policies={"space.fanout_limit": raw}).api
        assert api._space_fanout_limit() == 8


def test_search_spaces_merges_results_from_every_readable_space(api) -> None:
    api.add("项目部署在集群 A", identity=ALICE, coords={"project": "p1"})
    api.add("偏好深色主题", identity=ALICE)
    result = api.search_spaces("部署 主题", Context(scope=Scope(org=ORG)), identity=ALICE, top_k=10)
    contents = {item.content for item in result.items}
    assert contents == {"项目部署在集群 A", "偏好深色主题"}


def test_search_spaces_drops_spaces_the_caller_cannot_read(api) -> None:
    """无权空间直接剔除，不报错——一次跨空间调用不因某个空间无权而整体失败。"""
    api.add("项目部署在集群 A", identity=ALICE, coords={"project": "p1"})
    api.add("bob 的私人偏好", Scope(org=ORG, space=BOB_SPACE), identity=BOB)
    result = api.search_spaces(
        "部署 偏好",
        Context(scope=Scope(org=ORG)),
        identity=ALICE,
        spaces=[PROJECT_SPACE, BOB_SPACE],
        top_k=10,
    )
    contents = {item.content for item in result.items}
    assert "bob 的私人偏好" not in contents
    assert "项目部署在集群 A" in contents


def test_search_spaces_narrows_by_the_second_family_of_predicates(api) -> None:
    """第二族按上下文收窄：坐标指向别的项目时，该项目的条目不进结果。

    标签取值为空串的条目一并命中，因此「该维不适用」的内容不会被收窄掉。
    """
    api.add("项目部署在集群 A", identity=ALICE, coords={"project": "p1"})
    api.add("偏好深色主题", identity=ALICE)
    narrowed = api.search_spaces(
        "部署 主题",
        Context(scope=Scope(org=ORG)),
        identity=ALICE,
        spaces=[ALICE_SPACE, PROJECT_SPACE],
        coords={"project": "p2"},
        top_k=10,
    )
    contents = {item.content for item in narrowed.items}
    assert "项目部署在集群 A" not in contents


def test_an_absent_coordinate_does_not_narrow(api) -> None:
    """坐标缺项不生成对应谓词，表现为该维不收窄——失效方向是放宽，不是越权。"""
    api.add("项目部署在集群 A", identity=ALICE, coords={"project": "p1"})
    result = api.search_spaces(
        "部署",
        Context(scope=Scope(org=ORG)),
        identity=ALICE,
        spaces=[PROJECT_SPACE],
        top_k=10,
    )
    assert [item.content for item in result.items] == ["项目部署在集群 A"]


def test_search_spaces_truncates_the_candidate_set_at_the_fanout_limit(api) -> None:
    """上限截断不静默：超出上限的空间不参与召回，截断数落审计。"""
    from jiuwen_memory.api.memory_api_impl import collective

    extra = [f"x{index}" for index in range(collective.SPACE_FANOUT_LIMIT + 3)]
    candidates = api._search_candidates(ALICE, ORG, [PROJECT_SPACE, *extra])
    assert len(candidates) == collective.SPACE_FANOUT_LIMIT
    assert candidates[0] == PROJECT_SPACE


def _result(contents: list[str]):
    from jiuwen_memory.retrieval import RetrievalResult
    from jiuwen_memory.retrieval.types import RetrievedItem

    return RetrievalResult(
        items=[RetrievedItem(unit_id=f"id-{item}", content=item) for item in contents]
    )


def test_each_space_fetches_up_to_top_k_rather_than_a_fixed_share() -> None:
    """取数是上界不是定额：定额的缺口不回流，表现为静默少返回。"""
    from jiuwen_memory.api.memory_api_impl import collective

    assert collective.allocate_quota([ALICE_SPACE, PROJECT_SPACE], 10) == {
        ALICE_SPACE: 10,
        PROJECT_SPACE: 10,
    }


def test_the_total_fetch_is_capped_when_top_k_is_large() -> None:
    """空间数上限封住空间数，取数总量上限封住 top_k 很大的调用。"""
    from jiuwen_memory.api.memory_api_impl import collective

    spaces = [f"s{index}" for index in range(collective.SPACE_FANOUT_LIMIT)]
    quota = collective.allocate_quota(spaces, 1000)
    assert sum(quota.values()) == collective.TOTAL_FETCH_CAP


def test_a_space_that_runs_out_hands_its_slots_to_the_others() -> None:
    """轮转的第一处收益：队列取空即本轮跳过，未用完的名额流给仍有内容的空间。

    定额分配下这些名额空置——实测 20 条 + 1 条、top_k=10 时只返回 6 条。
    """
    from jiuwen_memory.api.memory_api_impl import collective

    merged = collective.merge(
        [
            (ALICE_SPACE, _result([f"a{index}" for index in range(10)])),
            (PROJECT_SPACE, _result(["b0", "b1", "b2"])),
        ],
        top_k=10,
        priority=[ALICE_SPACE, PROJECT_SPACE],
    )
    assert len(merged.items) == 10
    assert [item.content for item in merged.items[:6]] == ["a0", "b0", "a1", "b1", "a2", "b2"]


def test_a_duplicate_consumes_the_queue_but_not_a_slot() -> None:
    """轮转的第二处收益：重复内容不占 top_k 名额，从同一队列继续向下取。"""
    from jiuwen_memory.api.memory_api_impl import collective

    shared = ["x1", "x2", "x3"]
    merged = collective.merge(
        [
            (ALICE_SPACE, _result([*shared, *(f"a{index}" for index in range(4, 11))])),
            (PROJECT_SPACE, _result([*shared, *(f"b{index}" for index in range(4, 11))])),
        ],
        top_k=10,
        priority=[ALICE_SPACE, PROJECT_SPACE],
    )
    assert len(merged.items) == 10
    assert [item.content for item in merged.items].count("x1") == 1


def test_every_space_with_content_gets_a_slot_before_any_space_gets_a_second_round() -> None:
    """轮转保证的是覆盖面：靠前的空间不会独占 top_k。

    顺序拼接下 ``PROJECT_SPACE`` 整个不出现——各空间取 top_k 之后首个空间即填满。
    """
    from jiuwen_memory.api.memory_api_impl import collective

    merged = collective.merge(
        [
            (ALICE_SPACE, _result([f"a{index}" for index in range(10)])),
            (PROJECT_SPACE, _result([f"b{index}" for index in range(10)])),
        ],
        top_k=4,
        priority=[ALICE_SPACE, PROJECT_SPACE],
    )
    assert [item.content for item in merged.items] == ["a0", "b0", "a1", "b1"]


def test_merging_stops_when_every_remaining_item_is_a_duplicate() -> None:
    """全部队列只剩重复内容时停止，不空转。"""
    from jiuwen_memory.api.memory_api_impl import collective

    merged = collective.merge(
        [(ALICE_SPACE, _result(["x", "y"])), (PROJECT_SPACE, _result(["x", "y"]))],
        top_k=10,
        priority=[ALICE_SPACE, PROJECT_SPACE],
    )
    assert [item.content for item in merged.items] == ["x", "y"]


def test_a_failed_space_keeps_its_channel_errors_in_the_merged_result() -> None:
    """轮转不改变 errors 的归并：某个空间的通道失败不在跨空间调用里消失。"""
    from jiuwen_memory.api.memory_api_impl import collective
    from jiuwen_memory.retrieval import RetrievalResult

    failed = RetrievalResult()
    failed.errors.append("channel down")
    merged = collective.merge(
        [(ALICE_SPACE, _result(["a0"])), (PROJECT_SPACE, failed)],
        top_k=10,
        priority=[ALICE_SPACE, PROJECT_SPACE],
    )
    assert merged.errors == ["channel down"]


# -- 按谓词的批量删除 ------------------------------------------------------ #
#
# 实体删除的连带清理不由内核编排，由接入方按业务侧的实体关系逐个调 ``delete``（S09
# 「删除连带不由内核编排」）。这里测的是内核为此提供的那一项能力：``DeleteSelector``
# 的结构化谓词，它使「按作者主体标记」与「按判定标签」的批量删除可表达——两者都落在
# 条目 metadata 上，原有的 ``tags``（标签数组）表达不了。


def test_entries_written_by_one_author_are_removable_from_a_shared_space(api) -> None:
    """用户注销的第二步：按作者主体标记清除他在协作空间写的条目，他人的条目不动。

    作者标记由内核按身份写入、伪造不了，因此这一步用它而不用判定标签。
    """
    api.add("项目部署在集群 A", identity=ALICE, coords={"project": "p1"})
    api.add("bob 记的项目事实", Scope(org=ORG, space=PROJECT_SPACE), identity=BOB)

    removed = api.delete(
        DeleteSelector(
            scope=Scope(org=ORG, space=PROJECT_SPACE),
            filters=FilterClause("system_metadata.author_principal", FilterOp.EQ, "user:alice"),
            mode=DeleteMode.PURGE,
        ),
        identity=ALICE,
    )

    assert len(removed) == 1
    remaining = [unit.content for unit in _units_in(api, PROJECT_SPACE, BOB)]
    assert remaining == ["bob 记的项目事实"]


def test_entries_tagged_with_one_project_are_removable_and_untagged_ones_are_kept(api) -> None:
    """项目删除的第二步：按项目标签清除其余空间里的条目；标签为空的不删。

    「为空的不删」由等值谓词天然保证——空串与具体取值不相等。这一条是显式 scope 写入也
    要补齐标签键的前提：键缺失时该条目在任何按标签的谓词下都不匹配，删不掉也查不到。
    """
    api.add("偏好深色主题", identity=ALICE, coords={"project": "p1"})
    api.add("习惯早上工作", identity=ALICE)

    removed = api.delete(
        DeleteSelector(
            scope=Scope(org=ORG, space=ALICE_SPACE),
            filters=FilterClause("system_metadata.project_id", FilterOp.EQ, "p1"),
            mode=DeleteMode.PURGE,
        ),
        identity=ALICE,
    )

    assert len(removed) == 1
    assert [unit.content for unit in _units_in(api, ALICE_SPACE, ALICE)] == ["习惯早上工作"]


def test_a_non_member_cannot_delete_entries_by_predicate(api) -> None:
    """谓词删除不绕过鉴权：非成员对协作空间的删除按空间鉴权拒绝。"""
    api.add("项目部署在集群 A", identity=ALICE, coords={"project": "p1"})
    carol = Scope(org=ORG, user="carol")

    with pytest.raises(PermissionDeniedError):
        api.delete(
            DeleteSelector(
                scope=Scope(org=ORG, space=PROJECT_SPACE),
                filters=FilterClause("system_metadata.author_principal", FilterOp.EQ, "user:alice"),
                mode=DeleteMode.PURGE,
            ),
            identity=carol,
        )


# -- 写入边界与落盘不变量的两条绕过通道（R04 D1 / D2）--------------------- #


def test_update_cannot_rewrite_the_route_tag_keys(api) -> None:
    """判定标签键在改写入口同样被拒。

    只挂写入入口时改写即绕过通道：内容 EDITOR 可把他人条目的会话标签改成自己的会话 id，
    或改写项目标签使条目脱离按标签的批量删除范围。
    """
    unit = api.add("项目部署在集群 A", identity=ALICE, coords={"project": "p1"})[0]

    with pytest.raises(ValidationError) as excinfo:
        api.update(
            unit.id,
            unit.scope,
            MemoryPatch(system_metadata={"project_id": "p9", "session_id": "s-other"}),
            identity=ALICE,
        )
    assert "project_id" in str(excinfo.value)


def test_both_write_paths_land_the_same_set_of_route_tag_keys(api) -> None:
    """显式 scope 与判定两条路径落盘的判定标签键集合相同。

    不变量的定义域是「落盘条目」而不是「判定产物」。不补齐时集合谓词在字段缺失处判为不
    匹配，带 coords 的检索静默漏掉显式 scope 写入的条目——调用方既不报错也拿不到。
    """
    api.add("偏好深色主题", Scope(org=ORG, space=ALICE_SPACE), identity=ALICE)
    api.add("习惯早上工作", identity=ALICE)

    keys = [
        {key for key in unit.system_metadata if key in api._route_table.tag_keys}
        for unit in _units_in(api, ALICE_SPACE, ALICE)
    ]
    assert len(keys) == 2
    assert keys[0] == keys[1] == set(api._route_table.tag_keys)


def test_a_narrowed_search_recalls_entries_written_with_an_explicit_scope(api) -> None:
    """带 coords 的检索能召回显式 scope 写入的条目——标签为空串，一并命中。"""
    api.add("偏好深色主题", Scope(org=ORG, space=ALICE_SPACE), identity=ALICE)

    result = api.search_spaces(
        "深色主题",
        Context(scope=Scope(org=ORG)),
        identity=ALICE,
        coords={"project": "p1"},
        top_k=10,
    )
    assert [item.content for item in result.items] == ["偏好深色主题"]


# -- 跨空间检索的两处对齐（R04 D5 / D6）---------------------------------- #


def test_search_spaces_reinjects_the_routing_values_like_the_single_space_entry(api) -> None:
    """授权所依据的路由值在跨空间入口同样回注为系统谓词。

    只挂单空间入口时，`search_spaces` 即该绑定的绕过通道：路由值填宽松策略对应的类型、
    filters 指向受严格策略保护的数据，即可用 A 的钥匙开 B 的门。
    """
    seen: list[FilterExpr | None] = []
    original = api._engine.recall

    async def _capture(scope, query):
        seen.append(query.filters)
        return await original(scope, query)

    api._engine.recall = _capture
    api._perm.routing_fields = lambda: ("memory_type",)
    try:
        api.search_spaces(
            "深色主题",
            Context(scope=Scope(org=ORG), extensions={"memory_type": "notes"}),
            identity=ALICE,
            spaces=[ALICE_SPACE],
            top_k=5,
        )
    finally:
        api._engine.recall = original

    assert seen, "引擎未被调用"
    clauses = [clause for expr in seen for clause in iter_clauses(expr)]
    assert any(
        clause.op is FilterOp.EQ and str(clause.value) == "notes" for clause in clauses
    ), f"路由值未回注：{clauses}"


def test_a_failing_space_surfaces_a_channel_error_instead_of_vanishing(api) -> None:
    """单个空间的通道失败产出一条 ChannelError，不静默丢弃。

    只记审计日志时，调用方无从区分「这个空间里没有内容」与「这个空间挂了」，而两者的后续
    动作完全不同。
    """
    api.add("项目部署在集群 A", identity=ALICE, coords={"project": "p1"})
    original = api._engine.recall

    async def _flaky(scope, query):
        if scope.space == PROJECT_SPACE:
            raise RuntimeError("backend down")
        return await original(scope, query)

    api._engine.recall = _flaky
    try:
        result = api.search_spaces(
            "部署",
            Context(scope=Scope(org=ORG)),
            identity=ALICE,
            spaces=[ALICE_SPACE, PROJECT_SPACE],
            top_k=5,
        )
    finally:
        api._engine.recall = original

    assert [error.source for error in result.errors] == [PROJECT_SPACE]
    assert result.errors[0].channel is RecallChannel.SPACE
    assert result.errors[0].error_type == "RuntimeError"


# -- 批量入口每批一次判定（R04 S1）--------------------------------------- #


def test_a_batch_is_routed_in_one_call_rather_than_once_per_item(api) -> None:
    """N 条输入对应 1 次 `Router.route`。

    逐条送判使 `Router` 契约的「每批一次模型调用」在批量入口整段失效：20 条即 20 次串行
    模型调用，同批条目的判据也可以不一致，模型实现内部的分批随之失效。
    """
    calls: list[int] = []
    original = api._router.route

    def _counted(units, ctx):
        calls.append(len(units))
        return original(units, ctx)

    api._router.route = _counted
    try:
        result = api.batch_add(
            [
                BatchWriteItem(content=text)
                for text in ("偏好深色主题", "项目部署在集群 A", "团队要两人评审")
            ],
            identity=ALICE,
            coords={"project": "p1"},
        )
    finally:
        api._router.route = original

    assert not any(outcome.error for outcome in result.outcomes)
    assert calls == [3]
