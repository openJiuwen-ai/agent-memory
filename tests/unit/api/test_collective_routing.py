"""归属判定与多空间读写的端到端行为（F07「多空间读写」，F07 分能力用例）。

判定表与两个落盘不变量的单测在 ``tests/unit/construction/test_router_table.py``；本文件测
的是接线：写入侧的候选空间集合与落点、检索侧的两族谓词与跨空间合并、按谓词的批量删除。

判定实现取一个按关键词作答的桩：模型实现的输出不可复现，而本文件断言的是接线，不是模型
判得准不准。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import NotFoundError, PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.legacy import legacy_request_context
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
CAROL = Scope(org=ORG, user="carol")  # 不参与任何空间，用于「候选集为空」一例

# 接口先行过渡桥接：identity Scope 包成 RequestSecurityContext（安全实装合入后随接口一并改）
SEC_ALICE = legacy_request_context(ALICE)
SEC_ALICE_VIA_A1 = legacy_request_context(ALICE_VIA_A1)
SEC_BOB = legacy_request_context(BOB)
SEC_OPS = legacy_request_context(OPS)


# 归属坐标经参数袋传入、不占形参（F07 「归属坐标的承载」）。同一取值在本模块高频复用，
# 抽成常量：每处重复字面量既拉长调用行，也让「坐标取值变了」与「坐标形态变了」两类改动
# 混在一起。取值不就地修改——API 层取出时做的是拷贝。
COORDS_P1 = {"coords": {"project": "p1"}}
# 请求判定但本次没有业务坐标可给。键在参数袋里即「落点交给判定」，值为空只说明这次
# 落不到任何业务实体上——判定链的内核坐标由身份填入，不依赖调用方给值。
NO_COORDS: dict = {"coords": {}}

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
    engine_params = {name: "default" for name in _ENGINE_COMPONENT_NAMES}
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
    api.create_space(SpaceSpec(org=ORG, space=PROJECT_SPACE, owner=ALICE), security=SEC_OPS)
    if with_bob:
        api.add_space_member(
            ORG, PROJECT_SPACE, _member("bob", SpaceContentRole.EDITOR), security=SEC_ALICE
        )


@pytest.fixture
def api():
    kernel = _kernel()
    api = kernel.api
    api.create_space(SpaceSpec(org=ORG, space=ALICE_SPACE, owner=ALICE), security=SEC_OPS)
    api.create_space(SpaceSpec(org=ORG, space=BOB_SPACE, owner=BOB), security=SEC_OPS)
    _open_project_space(api)
    return api


def _units_in(api, space: str, identity: Scope) -> list[MemoryUnit]:
    return api.list(
        Scope(org=ORG, space=space),
        security=legacy_request_context(identity),
        limit=100,
    ).items


def test_the_api_layer_and_the_construction_layer_share_one_router(api) -> None:
    """两层取的是同一个具名实例，因而共用一份判定表。

    各建一份的后果是写入边界拒绝的键集合与判定实际写入的键集合可以不一致——判定自己写的
    标签会在下一次写入时被自己的边界校验拒绝。
    """
    assert api._router is not None
    assert api._engine._evolver._router is api._router
    assert api._route_table.tag_keys == {
        "agent_id",
        "session_id",
        "project_id",
        "team_id",
        # 收窄维各带一个归属未决派生键；记录维（team_id）不派生。
        "agent_id_unresolved",
        "session_id_unresolved",
        "project_id_unresolved",
    }


# -- 写入时自动选空间 ------------------------------------------------------ #


def test_a_given_scope_is_not_routed(api) -> None:
    """传了 scope 就是它，判定不介入——两条路径互斥、不叠加。"""
    api.add("项目部署在集群 A", Scope(org=ORG, space=ALICE_SPACE), security=SEC_ALICE)
    assert len(_units_in(api, ALICE_SPACE, ALICE)) == 1
    assert not _units_in(api, PROJECT_SPACE, ALICE)


def test_an_omitted_scope_is_routed_into_the_project_space(api) -> None:
    """scope 不给 space 维即按归属坐标判落点：项目事实落协作空间，不落个人主空间。"""
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    landed = _units_in(api, PROJECT_SPACE, ALICE)
    assert [unit.content for unit in landed] == ["项目部署在集群 A"]
    assert landed[0].system_metadata["memory_class"] == "project_memory"
    assert not _units_in(api, ALICE_SPACE, ALICE)


def test_a_user_fact_lands_in_the_individual_space(api) -> None:
    api.add("偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    landed = _units_in(api, ALICE_SPACE, ALICE)
    assert [unit.system_metadata["memory_class"] for unit in landed] == ["user_memory"]


def test_a_record_only_class_lands_in_fallback_and_keeps_its_tag(api) -> None:
    """记录维类别不落独立空间：落 fallback，实体记成标签。"""
    api.add(
        "团队用同一套评审流程", scope=Scope(org=ORG), security=SEC_ALICE,
        system_metadata={"coords": {"team": "t1"}},
    )
    landed = _units_in(api, ALICE_SPACE, ALICE)
    assert landed[0].system_metadata["memory_class"] == "team_memory"
    assert landed[0].system_metadata["team_id"] == "t1"


def test_all_routing_tag_keys_are_written_even_without_a_business_coordinate(api) -> None:
    """键恒存在：坐标里没有 project 时该键仍写空串，否则集合谓词把条目静默筛掉。"""
    api.add("偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=NO_COORDS)
    landed = _units_in(api, ALICE_SPACE, ALICE)[0]
    assert landed.system_metadata["project_id"] == ""
    assert landed.system_metadata["team_id"] == ""
    assert landed.system_metadata["session_id"] == ""


def test_routing_cannot_reach_a_space_the_caller_cannot_write(api) -> None:
    """判定不得扩权：坐标指向的空间无写权时它不进候选集，内容落 fallback。

    bob 不是 p1 的参与者时把坐标填成 p1，仍只能落自己的主空间。
    """
    api.remove_space_member(ORG, PROJECT_SPACE, Scope(org=ORG, user="bob"), security=SEC_ALICE)
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_BOB, system_metadata=COORDS_P1)
    assert not _units_in(api, PROJECT_SPACE, ALICE)
    assert len(_units_in(api, BOB_SPACE, BOB)) == 1


def test_the_fallback_space_is_created_on_first_write(api) -> None:
    """fallback 空间不存在即按调用方身份自动创建并登记归属（默认开）。

    空间名由调用方自己的身份渲染而来，别的主体渲染不出它，归属该登记给谁是确定的。
    """
    kernel = _kernel()  # 一个空间都没建
    fresh = kernel.api
    units = fresh.add(
        "偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1
    )
    assert units[0].scope.space == ALICE_SPACE
    info = fresh.get_space(ORG, ALICE_SPACE, security=SEC_ALICE)
    assert [owner.user for owner in info.owners] == ["alice"]
    # 只建 fallback：坐标指向的协作空间不自动建——它的成员表内核产生不了。
    with pytest.raises(NotFoundError):
        kernel.space.get(ORG, PROJECT_SPACE)


def test_auto_create_is_switchable_off() -> None:
    """关掉开关即回到硬前置：接入方须在用户开通时预建主空间。"""
    kernel = build_kernel(
        config=Config.from_dict(
            {
                "engine": {
                    "default": {
                        "target": "cloud",
                        "params": {
                            name: "default" for name in _ENGINE_COMPONENT_NAMES
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
        api.add(
            "项目部署在集群 A",
            scope=Scope(org=ORG),
            security=SEC_ALICE,
            system_metadata=COORDS_P1,
        )
    assert not api.list(
        Scope(org=ORG, space=PROJECT_SPACE), security=SEC_ALICE, limit=10
    ).items


def test_auto_create_does_not_resurrect_a_space_the_caller_cannot_write(api) -> None:
    """自动创建只处理「还没开通」，不处理「不让你写」。

    归档自己的主空间后再写入仍被拒，空间状态不被覆盖——否则任何一次写入都能把归档
    撤销，归档这条约束形同虚设。
    """
    api.archive_space(ORG, ALICE_SPACE, security=SEC_ALICE)
    with pytest.raises(ValidationError, match="not writable"):
        api.add("偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    assert api.get_space(ORG, ALICE_SPACE, security=SEC_ALICE).status is SpaceStatus.ARCHIVED


def test_the_coordinates_key_is_what_requests_a_decision_not_an_empty_space() -> None:
    """分流判据是参数袋里有没有 ``coords`` 键，不是 ``scope.space`` 的取值形态。

    拿 ``space`` 为空作判据即由内核解读一个缺省状态：调用方什么都没表达，整条处理路径却
    因此改道。键则是调用方为这件事主动放进去的，键在即请求判定。
    """
    routed = _kernel().api
    routed.create_space(SpaceSpec(org=ORG, space=ALICE_SPACE, owner=ALICE), security=SEC_OPS)
    units = routed.add(
        "偏好深色主题", Scope(org=ORG), security=SEC_ALICE, system_metadata=NO_COORDS
    )
    assert units[0].scope.space == ALICE_SPACE, "键在即交给判定"

    with pytest.raises(ValidationError, match="写入落点未声明"):
        routed.add("偏好深色主题", Scope(org=ORG), security=SEC_ALICE)


def test_an_empty_space_without_a_decision_request_is_rejected_only_where_assembled() -> None:
    """装配条件不可省：未装配判定算子的部署里 ``space`` 为空是既有的合法落点。

    本地栈的 ``InMemoryEngine`` 要求 ``space`` 为空串，那批调用的落点正是它。少了这个
    条件，它们会撞上「写入落点未声明」，是破坏性变更。
    """
    plain = build_kernel().api
    landed = plain.add("偏好深色主题", Scope(org=ORG, user="alice"), security=SEC_ALICE)
    assert landed[0].scope.space == "", "未装配判定表：space 为空照旧直写，行为与改造前一致"


def test_a_decision_request_outranks_a_given_space(api) -> None:
    """给了 ``coords`` 即交出落点决定权，``scope.space`` 不参与落点计算。

    以 ``space`` 非空否决判定请求，按自己的租户或应用标识填 ``space`` 的接入方将无路可走：
    那个取值不是本系统的空间标识，直写要求空间已登记，未登记即判权拒绝，内核又不为调用方
    给的空间名自动创建。真实落点由返回的记忆单元携带，不靠调用方推断。
    """
    units = api.add(
        "项目部署在集群 A",
        Scope(org=ORG, space="upstream_tenant_42"),
        security=SEC_ALICE,
        system_metadata=COORDS_P1,
    )
    assert units[0].scope.space == PROJECT_SPACE
    assert not _units_in(api, ALICE_SPACE, ALICE)


def test_the_decision_path_still_checks_the_other_scope_dimensions(api) -> None:
    """走判定路径时入参 ``scope`` 的其余维不参与落点计算，但仍须与身份一致。

    这条路径上 ``org`` 取自身份、主体维在落盘 scope 上恒为空，两者都不生效。不校验即
    静默丢弃调用方的声明——写 ``user=bob`` 会落进调用方自己的主空间，从调用侧看不出
    与预期的差别。``space`` 维不在此列：它同样不参与落点，但那是判定请求的既定语义。
    """
    with pytest.raises(ValidationError, match="does not match the caller identity"):
        api.add(
            "偏好深色主题", Scope(org=ORG, user="bob"), security=SEC_ALICE,
            system_metadata=NO_COORDS,
        )
    with pytest.raises(ValidationError, match="does not match the caller identity"):
        api.add(
            "偏好深色主题", Scope(org="other_org"), security=SEC_ALICE,
            system_metadata=NO_COORDS,
        )


def test_a_missing_scope_is_a_missing_argument_not_a_routing_request() -> None:
    """``scope`` 保持必填：漏传即缺参，调用处即可发现。

    这是接口整改要保住的那条区分。改造过程中曾用「省略 ``scope``」表达「交给判定」，二者
    因而共用同一个信号，静态检查无从区分，漏传要到运行期、且只在未装配判定的部署上才报错。
    现在请求判定是往参数袋里放 ``coords`` 键，与 ``scope`` 是两个入参，漏传仍是缺参。
    """
    api = _kernel(with_router=False).api
    with pytest.raises(TypeError, match="scope"):
        api.add("偏好深色主题", security=SEC_ALICE)


def test_a_routing_table_without_space_authorization_is_rejected_at_assembly() -> None:
    """判定表已配置而判定实现不读空间事实，装配期即拒绝（R06 第二章「四种组合」）。

    该组合把内容按坐标分流进协作空间，而协作空间没有任何权限边界，等于把内容写进一个
    组织内任意主体可读的位置。方向为放行，且从调用侧看不出异常——写入成功、检索也拿得到，
    只是拿得到的人多了。
    """
    engine_params = {name: "default" for name in _ENGINE_COMPONENT_NAMES}
    config = {
        "engine": {"default": {"target": "cloud", "params": engine_params}},
        "router": {"default": {"target": "keyword_stub", "params": dict(ROUTE_TABLE)}},
    }
    # 断言取修复动作而非现象描述：报错首句须给出改法，日志截断时正好丢掉指引。
    with pytest.raises(ValidationError, match="permission.default.target 配成 space_aware"):
        build_kernel(config=Config.from_dict(config))


def test_space_authorization_without_a_routing_table_assembles() -> None:
    """反方向是合法部署：空间之间有权限边界，``scope`` 仍必填。"""
    assert _kernel(with_router=False).api is not None


def test_batch_items_without_a_scope_are_routed_per_item(api) -> None:
    """判定按写入路径生效、不按单条与批量入口区分。

    缺这一条时批量入口的空 ``space`` 停在「缺参」上，``coords`` 加在它上面没有实际作用。
    """
    result = api.batch_add(
        [BatchWriteItem(content="项目部署在集群 A"), BatchWriteItem(content="偏好深色主题")],
        scope=Scope(org=ORG), security=SEC_ALICE,
        system_metadata=COORDS_P1,
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
    api.batch_add(
        [BatchWriteItem(content="项目部署在集群 A"), BatchWriteItem(content="偏好深色主题")],
        scope=Scope(org=ORG), security=SEC_ALICE,
        system_metadata=COORDS_P1,
    )
    batches = [ids for ids in api._router.seen_ids if len(ids) > 1]
    assert batches, "批量入口应把 space 为空的两条一次送判"
    ids = batches[-1]
    assert all(ids), "探针 id 不得为空"
    assert len(set(ids)) == len(ids), "同批探针 id 不得重复"


def test_the_derived_units_of_an_infer_write_are_routed_in_the_construction_layer(api) -> None:
    """同步抽取路径：判定在构建层逐条进行，上下文经源单元的瞬态键传下去。

    这条路径与单条判定入口分工不同——它判的是抽取产出的派生单元，而不是调用方给的原文。
    """
    units = api.add(
        "项目部署在集群 A",
        scope=Scope(org=ORG), security=SEC_ALICE,
        system_metadata={"infer": "true", "coords": {"project": "p1"}},
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
        scope=Scope(org=ORG), security=SEC_ALICE,
        system_metadata={"infer": "true", "coords": {"project": "p1"}},
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
        scope=Scope(org=ORG), security=SEC_ALICE,
        system_metadata={"infer": "true", "coords": {"project": "p1"}},
    )
    assert seen, "写入鉴权上下文未被构造，用例前提不成立"
    assert all("route_ctx" not in metadata for metadata in seen)


def test_the_returned_units_of_a_routed_infer_write_are_readable_by_id(api) -> None:
    """落盘产物按回传对象取，不按入参 scope 回读——判定改了 scope，按原 scope 读会落空。"""
    units = api.add(
        "项目部署在集群 A",
        scope=Scope(org=ORG), security=SEC_ALICE,
        system_metadata={"infer": "true", "coords": {"project": "p1"}},
    )
    fetched = api.get(units[0].id, units[0].scope, security=SEC_ALICE)
    assert fetched.id == units[0].id


# -- 写入边界校验 ---------------------------------------------------------- #


def test_the_caller_cannot_assign_a_kernel_coordinate(api) -> None:
    """内核三项坐标取自调用方身份，赋值即拒绝。

    静默丢弃的话调用方以为自己指定了归属、实际被忽略，而落点与检索结果都会与预期不符且
    没有提示。判定表的加载期第 12 条已禁止配置声明这三项，调用层跟着拒绝才是同一口径。
    """
    with pytest.raises(ValidationError, match="不得给内核坐标赋值"):
        api.add(
            "偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE,
            system_metadata={"coords": {"user": "bob"}},
        )
    with pytest.raises(ValidationError, match="不得给内核坐标赋值"):
        api.search(
            "深色主题",
            Context(
                scope=Scope(org=ORG, space=ALICE_SPACE),
                extensions={"coords": {"session": "s9"}},
            ),
            security=SEC_ALICE,
        )


def test_the_coordinates_do_not_reach_the_stored_unit_or_the_permission_context(api) -> None:
    """坐标是判定输入，不是条目属性：既不落盘，也不进写入鉴权上下文。

    两处都要挡。落盘的后果是每个条目多带一个嵌套字典字段——按 §3.2 的判据，
    Elasticsearch 会把它逐层展开成新 mapping。进鉴权上下文的后果是取值被 ``str()``
    成 ``"{'project': 'p1'}"``，既污染判据，又可能与某条 policy 的路由字段撞上。
    """
    seen: list[dict[str, str]] = []
    original = api._perm.decide

    def _capture(identity, target, action, *, context=None, **kwargs):
        if context is not None and context.resource_type == "write_input":
            seen.append(dict(context.metadata))
        return original(identity, target, action, context=context, **kwargs)

    api._perm.decide = _capture
    units = api.add(
        "项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1
    )

    assert seen, "写入鉴权上下文未被构造，用例前提不成立"
    assert all("coords" not in metadata for metadata in seen)
    assert all("coords" not in unit.system_metadata for unit in units)
    assert all(
        "coords" not in unit.system_metadata
        for unit in _units_in(api, PROJECT_SPACE, ALICE)
    )


def test_the_coordinates_do_not_reach_the_retrieval_module(api) -> None:
    """检索侧的坐标取出后即从透传 options 移除，不进自定义检索模块的入参。

    与 ``max_tokens`` 同一处置。留下的后果是坐标以一个未声明的字段出现在该模块入参里，
    而模块对它没有约定——取值形态一旦变化，表现为该模块行为改变且无从追溯。
    """
    seen: list[dict[str, object]] = []
    original = api._engine.recall

    async def _capture(scope, rq):
        seen.append(dict(rq.extensions))
        return await original(scope, rq)

    api._engine.recall = _capture
    api.search(
        "深色主题",
        Context(
            scope=Scope(org=ORG, space=ALICE_SPACE),
            extensions={"coords": {"project": "p1"}, "profile": "coding"},
        ),
        security=SEC_ALICE,
    )
    assert seen, "引擎召回未被调用，用例前提不成立"
    assert all("coords" not in extensions for extensions in seen)
    # 其余透传项照旧下传——移除的判据是「内核已解释」，不是「一律清空」。
    assert all(extensions.get("profile") == "coding" for extensions in seen)


def test_a_malformed_coordinate_payload_is_rejected(api) -> None:
    """坐标判型在运行期做：形参形态下由类型注解承担的那层约束，改走参数袋后须补回。

    覆盖不到的只有键名拼写——内核区分不了「拼错」与「本次不带该坐标」，两者在入参上是
    同一形态，失效方向是该维不收窄。
    """
    for payload in ("p1", ["p1"], {"project": 1}, {1: "p1"}):
        with pytest.raises(ValidationError, match="coords"):
            api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE,
                    system_metadata={"coords": payload})


def test_a_batch_item_cannot_carry_its_own_coordinates(api) -> None:
    """逐项坐标即拒绝：判定上下文每批只算一次，坐标取自批级参数袋。

    静默忽略的失效方向是「以为逐项坐标生效了、其实没有」，从调用侧看不出差别——两条
    内容都按批级坐标判落点，与调用方的预期不同却不报错。
    """
    result = api.batch_add(
        [
            BatchWriteItem(
                content="项目部署在集群 A", system_metadata={"coords": {"project": "p2"}}
            ),
            BatchWriteItem(content="偏好深色主题"),
        ],
        scope=Scope(org=ORG),
        security=SEC_ALICE,
        system_metadata=COORDS_P1,
    )
    # 归一化期的拒绝按批量入口的既有语义逐项回传，不中止整批。
    assert result.outcomes[0].error_type == "ValidationError"
    assert "coords" in result.outcomes[0].error
    assert result.outcomes[1].error == ""


def test_the_caller_cannot_assign_a_routing_tag_key(api) -> None:
    """判定标签键参与检索过滤，调用方能自行赋值即可绕过收窄。"""
    with pytest.raises(ValidationError, match="判定标签 key"):
        api.add(
            "偏好深色主题",
            Scope(org=ORG, space=ALICE_SPACE),
            security=SEC_ALICE,
            system_metadata={"session_id": "别人的会话"},
        )


def test_the_caller_cannot_declare_a_foreign_principal_on_the_write_scope(api) -> None:
    """归属不由调用方声明：入参 scope 的主体维与身份不一致即拒绝。"""
    with pytest.raises(ValidationError, match="does not match the caller identity"):
        api.add(
            "偏好深色主题",
            Scope(org=ORG, space=ALICE_SPACE, user="bob"),
            security=SEC_ALICE,
        )


def test_the_landed_scope_keeps_only_org_and_space(api) -> None:
    """条目落盘 scope 归一为两维：主体维留在键上会使判定按各维相等放行。"""
    api.add("偏好深色主题", Scope(org=ORG, space=ALICE_SPACE, session="s1"), security=SEC_ALICE)
    landed = _units_in(api, ALICE_SPACE, ALICE)[0]
    assert landed.scope == Scope(org=ORG, space=ALICE_SPACE)


# -- 跨空间检索 ------------------------------------------------------------ #


def test_the_fanout_limit_is_configurable_and_bounds_both_sides() -> None:
    """一次调用参与的空间数上限取自策略 ``space.fanout_limit``，写入侧与检索侧同值。

    这是功能天花板而非性能参数：主体同时参与的空间超过该值时，超出部分写入不进候选、
    检索也取不到。够用与否取决于接入方的协作规模假设，因此可配置而不写死在内核里。
    """
    api = _kernel(policies={"space.fanout_limit": "1"}).api
    api.create_space(SpaceSpec(org=ORG, space=ALICE_SPACE, owner=ALICE), security=SEC_OPS)
    _open_project_space(api, with_bob=False)

    # 写入侧：候选被截到只剩 fallback，项目内容也落 fallback。
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
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


def test_a_cross_space_search_merges_results_from_every_readable_space(api) -> None:
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    api.add("偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=NO_COORDS)
    result = api.search(
        "部署 主题",
        Context(scope=Scope(org=ORG), extensions={"spaces": []}),
        security=SEC_ALICE,
        top_k=10,
    )
    contents = {item.content for item in result.items}
    assert contents == {"项目部署在集群 A", "偏好深色主题"}


def test_a_cross_space_search_records_the_spaces_the_caller_cannot_read(api) -> None:
    """无权空间剔除后记进 errors：一次跨空间调用不因某个空间无权而整体失败，但也不静默。

    只记审计日志时，「这个空间我读不到」与「这个空间里没有内容」在返回值上是同一形态，
    而两者的后续动作完全不同。
    """
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    api.add("bob 的私人偏好", Scope(org=ORG, space=BOB_SPACE), security=SEC_BOB)
    result = api.search(
        "部署 偏好",
        Context(scope=Scope(org=ORG), extensions={"spaces": [PROJECT_SPACE, BOB_SPACE]}),
        security=SEC_ALICE,
        top_k=10,
    )
    contents = {item.content for item in result.items}
    assert "bob 的私人偏好" not in contents
    assert "项目部署在集群 A" in contents
    denied = [error for error in result.errors if error.source == BOB_SPACE]
    assert len(denied) == 1
    assert denied[0].channel is RecallChannel.SPACE
    assert denied[0].error_type == "PermissionDeniedError"


def test_a_cross_space_search_raises_when_no_candidate_space_is_readable(api) -> None:
    """候选集非空而一个都读不到时抛，与单空间路径同一处置。

    静默返回空即同一个方法的两条路径对「完全无权」给出两种结果，且调用方无从区分
    「一个都读不到」与「这些空间里没有内容」。候选集为空不抛——那是合法的空结果。
    """
    api.add("bob 的私人偏好", Scope(org=ORG, space=BOB_SPACE), security=SEC_BOB)
    with pytest.raises(PermissionDeniedError):
        api.search(
            "偏好",
            Context(scope=Scope(org=ORG), extensions={"spaces": [BOB_SPACE]}),
            security=SEC_ALICE,
            top_k=10,
        )


def test_an_empty_candidate_set_is_an_empty_result_not_a_denial(api) -> None:
    """主体不在任何空间里是合法的空结果，不是拒绝。"""
    result = api.search(
        "偏好",
        Context(scope=Scope(org=ORG), extensions={"spaces": []}),
        security=legacy_request_context(CAROL),
        top_k=10,
    )
    assert result.items == []
    assert result.errors == []


def test_the_spaces_key_is_what_turns_a_search_cross_space_not_an_empty_scope(api) -> None:
    """判据取键的有无：键不在即单空间检索，``scope.space`` 照旧决定查哪个空间。

    改按取值判空分流则「查我能读的全部空间」只能靠缺省状态表达，与「没打算跨空间」不可区分。
    """
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    api.add("偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=NO_COORDS)

    single = api.search(
        "部署 主题", Context(scope=Scope(org=ORG, space=ALICE_SPACE)), security=SEC_ALICE, top_k=10
    )
    assert {item.content for item in single.items} == {"偏好深色主题"}

    across = api.search(
        "部署 主题",
        Context(scope=Scope(org=ORG), extensions={"spaces": []}),
        security=SEC_ALICE,
        top_k=10,
    )
    assert {item.content for item in across.items} == {"项目部署在集群 A", "偏好深色主题"}


def test_a_cross_space_search_ignores_the_space_axis_of_the_context(api) -> None:
    """跨空间路径只取 ``context.scope`` 的 org 维，空间维传了不生效。"""
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    api.add("偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=NO_COORDS)
    result = api.search(
        "部署 主题",
        Context(scope=Scope(org=ORG, space=ALICE_SPACE), extensions={"spaces": []}),
        security=SEC_ALICE,
        top_k=10,
    )
    assert {item.content for item in result.items} == {"项目部署在集群 A", "偏好深色主题"}


@pytest.mark.parametrize("raw", [None, ALICE_SPACE, {"a": "b"}, [ALICE_SPACE, 1]])
def test_a_non_list_of_strings_for_spaces_is_a_validation_error(api, raw) -> None:
    """取值判型在运行期做：``None`` 一并拒绝，不当作空列表。

    网关把未填字段序列化成 ``null`` 时若按空列表处置，一次本意为单空间的检索会静默扩到
    调用方可读的全部空间——失效方向是放宽。
    """
    with pytest.raises(ValidationError):
        api.search(
            "部署",
            Context(scope=Scope(org=ORG), extensions={"spaces": raw}),
            security=SEC_ALICE,
            top_k=10,
        )


def test_the_spaces_key_does_not_reach_the_permission_context(api) -> None:
    """编排开关不进鉴权入参：留下会被 str 化成 "['u_alice']" 与某条 policy 的路由字段撞上。"""
    seen: list[dict] = []
    original = api._perm.decide

    def _capture(actor, target, action, context=None):
        if context is not None:
            seen.append(dict(context.metadata))
        return original(actor, target, action, context=context)

    api._perm.decide = _capture
    try:
        api.search(
            "部署",
            Context(scope=Scope(org=ORG), extensions={"spaces": [ALICE_SPACE]}),
            security=SEC_ALICE,
            top_k=10,
        )
    finally:
        api._perm.decide = original

    assert seen, "判定未被调用"
    assert all("spaces" not in metadata for metadata in seen), seen


def test_a_cross_space_search_narrows_by_the_second_family_of_predicates(api) -> None:
    """第二族按上下文收窄：坐标指向别的项目时，该项目的条目不进结果。

    标签取值为空串的条目一并命中，因此「该维不适用」的内容不会被收窄掉。
    """
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    api.add("偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=NO_COORDS)
    narrowed = api.search(
        "部署 主题",
        Context(
            scope=Scope(org=ORG),
            extensions={"coords": {"project": "p2"}, "spaces": [ALICE_SPACE, PROJECT_SPACE]},
        ),
        security=SEC_ALICE,
        top_k=10,
    )
    contents = {item.content for item in narrowed.items}
    assert "项目部署在集群 A" not in contents


def test_an_absent_coordinate_does_not_narrow(api) -> None:
    """坐标缺项不生成对应谓词，表现为该维不收窄——失效方向是放宽，不是越权。"""
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    result = api.search(
        "部署",
        Context(scope=Scope(org=ORG), extensions={"spaces": [PROJECT_SPACE]}),
        security=SEC_ALICE,
        top_k=10,
    )
    assert [item.content for item in result.items] == ["项目部署在集群 A"]


def test_search_narrows_by_the_session_dimension_taken_from_identity(api) -> None:
    """session 维的收窄取值只能来自身份。

    该维的坐标键被入口拒绝（``reject_kernel_coords``），调用方无从声明；取值由内核以身份
    覆盖折算而来。缺这一步表现为该维整体不收窄——别的会话的条目一并召回且不报错，失效
    方向是放宽。
    """
    alice_s1 = Scope(org=ORG, user="alice", session="s1")
    alice_s2 = Scope(org=ORG, user="alice", session="s2")
    api.add(
        "偏好深色主题",
        scope=Scope(org=ORG),
        security=legacy_request_context(alice_s1),
        system_metadata=NO_COORDS,
    )
    api.add(
        "偏好浅色主题",
        scope=Scope(org=ORG),
        security=legacy_request_context(alice_s2),
        system_metadata=NO_COORDS,
    )
    result = api.search(
        "偏好",
        Context(scope=Scope(org=ORG, space=ALICE_SPACE)),
        security=legacy_request_context(alice_s1),
        top_k=10,
    )
    contents = {item.content for item in result.items}
    assert "偏好深色主题" in contents
    assert "偏好浅色主题" not in contents


def test_a_cross_space_search_narrows_by_the_agent_dimension_taken_from_identity(api) -> None:
    """agent 维同上，且验的是跨空间入口——两个入口各自折算一次坐标，不共用。"""
    alice_a2 = Scope(org=ORG, user="alice", agent="a2")
    api.add(
        "偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE_VIA_A1,
        system_metadata=NO_COORDS,
    )
    api.add(
        "偏好浅色主题",
        scope=Scope(org=ORG),
        security=legacy_request_context(alice_a2),
        system_metadata=NO_COORDS,
    )
    result = api.search(
        "偏好",
        Context(scope=Scope(org=ORG), extensions={"spaces": [ALICE_SPACE]}),
        security=SEC_ALICE_VIA_A1,
        top_k=10,
    )
    contents = {item.content for item in result.items}
    assert "偏好深色主题" in contents
    assert "偏好浅色主题" not in contents


def test_a_cross_space_search_truncates_the_candidate_set_at_the_fanout_limit(api) -> None:
    """上限截断不静默：超出上限的空间不参与召回，截断数落审计。"""
    from jiuwen_memory.control import collective

    extra = [f"x{index}" for index in range(collective.SPACE_FANOUT_LIMIT + 3)]
    candidates = api._search_candidates(ALICE, ORG, [PROJECT_SPACE, *extra])
    assert len(candidates) == collective.SPACE_FANOUT_LIMIT
    assert candidates[0] == PROJECT_SPACE


# -- 按谓词的批量删除 ------------------------------------------------------ #
#
# 实体删除的连带清理不由内核编排，由接入方按业务侧的实体关系逐个调 ``delete``（F07
# 「删除连带不由内核编排」）。这里测的是内核为此提供的那一项能力：``DeleteSelector``
# 的结构化谓词，它使「按作者主体标记」与「按判定标签」的批量删除可表达——两者都落在
# 条目 metadata 上，原有的 ``tags``（标签数组）表达不了。


def test_entries_written_by_one_author_are_removable_from_a_shared_space(api) -> None:
    """用户注销的第二步：按作者主体标记清除他在协作空间写的条目，他人的条目不动。

    作者标记由内核按身份写入、伪造不了，因此这一步用它而不用判定标签。
    """
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    api.add("bob 记的项目事实", Scope(org=ORG, space=PROJECT_SPACE), security=SEC_BOB)

    removed = api.delete(
        DeleteSelector(
            scope=Scope(org=ORG, space=PROJECT_SPACE),
            filters=FilterClause("system_metadata.author_principal", FilterOp.EQ, "user:alice"),
            mode=DeleteMode.PURGE,
        ),
        security=SEC_ALICE,
    )

    assert len(removed) == 1
    remaining = [unit.content for unit in _units_in(api, PROJECT_SPACE, BOB)]
    assert remaining == ["bob 记的项目事实"]


def test_entries_tagged_with_one_project_are_removable_and_untagged_ones_are_kept(api) -> None:
    """项目删除的第二步：按项目标签清除其余空间里的条目；标签为空的不删。

    「为空的不删」由等值谓词天然保证——空串与具体取值不相等。这一条是显式 scope 写入也
    要补齐标签键的前提：键缺失时该条目在任何按标签的谓词下都不匹配，删不掉也查不到。
    """
    api.add("偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    api.add("习惯早上工作", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=NO_COORDS)

    removed = api.delete(
        DeleteSelector(
            scope=Scope(org=ORG, space=ALICE_SPACE),
            filters=FilterClause("system_metadata.project_id", FilterOp.EQ, "p1"),
            mode=DeleteMode.PURGE,
        ),
        security=SEC_ALICE,
    )

    assert len(removed) == 1
    assert [unit.content for unit in _units_in(api, ALICE_SPACE, ALICE)] == ["习惯早上工作"]


def test_a_non_member_cannot_delete_entries_by_predicate(api) -> None:
    """谓词删除不绕过鉴权：非成员对协作空间的删除按空间鉴权拒绝。"""
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    carol = Scope(org=ORG, user="carol")

    with pytest.raises(PermissionDeniedError):
        api.delete(
            DeleteSelector(
                scope=Scope(org=ORG, space=PROJECT_SPACE),
                filters=FilterClause("system_metadata.author_principal", FilterOp.EQ, "user:alice"),
                mode=DeleteMode.PURGE,
            ),
            security=legacy_request_context(carol),
        )


# -- 写入边界与落盘不变量的两条绕过通道（R04 D1 / D2）--------------------- #


def test_update_cannot_rewrite_the_route_tag_keys(api) -> None:
    """判定标签键在改写入口同样被拒。

    只挂写入入口时改写即绕过通道：内容 EDITOR 可把他人条目的会话标签改成自己的会话 id，
    或改写项目标签使条目脱离按标签的批量删除范围。
    """
    unit = api.add(
        "项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1
    )[0]

    with pytest.raises(ValidationError) as excinfo:
        api.update(
            unit.id,
            unit.scope,
            MemoryPatch(system_metadata={"project_id": "p9", "session_id": "s-other"}),
            security=SEC_ALICE,
        )
    assert "project_id" in str(excinfo.value)


def test_both_write_paths_land_the_same_set_of_route_tag_keys(api) -> None:
    """显式 scope 与判定两条路径落盘的判定标签键集合相同。

    不变量的定义域是「落盘条目」而不是「判定产物」。不补齐时集合谓词在字段缺失处判为不
    匹配，带 coords 的检索静默漏掉显式 scope 写入的条目——调用方既不报错也拿不到。
    """
    api.add("偏好深色主题", Scope(org=ORG, space=ALICE_SPACE), security=SEC_ALICE)
    api.add("习惯早上工作", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=NO_COORDS)

    keys = [
        {key for key in unit.system_metadata if key in api._route_table.tag_keys}
        for unit in _units_in(api, ALICE_SPACE, ALICE)
    ]
    assert len(keys) == 2
    assert keys[0] == keys[1] == set(api._route_table.tag_keys)


def test_a_narrowed_search_recalls_entries_written_with_an_explicit_scope(api) -> None:
    """带 coords 的检索能召回显式 scope 写入的条目——标签为空串，一并命中。"""
    api.add("偏好深色主题", Scope(org=ORG, space=ALICE_SPACE), security=SEC_ALICE)

    result = api.search(
        "深色主题",
        Context(
            scope=Scope(org=ORG),
            extensions={"coords": {"project": "p1"}, "spaces": []},
        ),
        security=SEC_ALICE,
        top_k=10,
    )
    assert [item.content for item in result.items] == ["偏好深色主题"]


# -- 跨空间检索的两处对齐（R04 D5 / D6）---------------------------------- #


def test_a_cross_space_search_reinjects_the_routing_values_like_the_single_space_path(api) -> None:
    """授权所依据的路由值在跨空间入口同样回注为系统谓词。

    只挂单空间路径时，跨空间路径即该绑定的绕过通道：路由值填宽松策略对应的类型、
    filters 指向受严格策略保护的数据，即可用 A 的钥匙开 B 的门。
    """
    seen: list[FilterExpr | None] = []
    original = api._engine.recall

    async def _capture(scope, query):
        seen.append(query.filters)
        return await original(scope, query)

    api._engine.recall = _capture

    def _routing_fields() -> tuple[str, ...]:
        return ("memory_type",)

    api._perm.routing_fields = _routing_fields
    try:
        api.search(
            "深色主题",
            Context(
                scope=Scope(org=ORG),
                extensions={"memory_type": "notes", "spaces": [ALICE_SPACE]},
            ),
            security=SEC_ALICE,
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
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    original = api._engine.recall

    async def _flaky(scope, query):
        if scope.space == PROJECT_SPACE:
            raise RuntimeError("backend down")
        return await original(scope, query)

    api._engine.recall = _flaky
    try:
        result = api.search(
            "部署",
            Context(
                scope=Scope(org=ORG),
                extensions={"spaces": [ALICE_SPACE, PROJECT_SPACE]},
            ),
            security=SEC_ALICE,
            top_k=5,
        )
    finally:
        api._engine.recall = original

    assert [error.source for error in result.errors] == [PROJECT_SPACE]
    assert result.errors[0].channel is RecallChannel.SPACE
    assert result.errors[0].error_type == "RuntimeError"


# -- 批量入口每批一次判定（R04 S1）--------------------------------------- #


def test_an_archived_candidate_falls_back_instead_of_failing_the_write(api) -> None:
    """归档的候选空间不进候选集，写入落到仍可写的 fallback。

    候选筛选只判权不判状态时，已归档的项目空间照样被选中，随后在写入处抛「空间不可写」
    ——此刻可写的 fallback 已不再被考虑，表现是整次写入失败而不是落到兜底空间。状态与
    权限须在同一步判，且取同一份事实快照。
    """
    api.archive_space(ORG, PROJECT_SPACE, security=SEC_ALICE)

    units = api.add(
        "项目 P1 的回滚脚本在 rollback/", scope=Scope(org=ORG), security=SEC_ALICE,
        system_metadata=COORDS_P1,
    )

    assert [unit.scope.space for unit in units] == [ALICE_SPACE]


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
            scope=Scope(org=ORG), security=SEC_ALICE,
            system_metadata=COORDS_P1,
        )
    finally:
        api._router.route = original

    assert not any(outcome.error for outcome in result.outcomes)
    assert calls == [3]


def test_a_batch_can_omit_the_batch_level_scope_when_every_item_carries_one() -> None:
    """批级 ``scope`` 是缺省值而非必填：逐项自带即可整批省略。

    该形态在本特性之前就可用，逐项归一按「项为 ``None`` 才回退到批级」进行。改造过程中
    曾把批级 ``scope`` 改成必填——那是为配合「省略 scope 即请求判定」的旧判据堵住信号歧义，
    判据改由 ``coords`` 键承载后该必填失去理由，签名因而回到改造前。
    """
    api = _kernel(with_router=False).api
    result = api.batch_add(
        [
            BatchWriteItem(content="第一条", scope=Scope(org=ORG, user="alice")),
            BatchWriteItem(content="第二条", scope=Scope(org=ORG, user="alice")),
        ],
        security=SEC_ALICE,
    )
    assert not any(outcome.error for outcome in result.outcomes)


def test_a_batch_item_without_any_scope_is_rejected_where_no_decision_is_requested() -> None:
    """两级 ``scope`` 都不给又没请求判定，仍是调用错误，措辞与改造前一致。"""
    api = _kernel(with_router=False).api
    result = api.batch_add([BatchWriteItem(content="第一条")], security=SEC_ALICE)
    assert result.outcomes[0].error is not None
    assert "batch item scope is required" in str(result.outcomes[0].error)


def test_a_batch_requests_the_decision_once_for_the_whole_batch(api) -> None:
    """请求判定是批级的：批级参数袋带 ``coords`` 即整批交给判定，两级 ``scope`` 都不必给。"""
    result = api.batch_add(
        [BatchWriteItem(content="偏好深色主题"), BatchWriteItem(content="项目部署在集群 A")],
        security=SEC_ALICE,
        system_metadata=COORDS_P1,
    )
    assert not any(outcome.error for outcome in result.outcomes)
    assert {unit.scope.space for outcome in result.outcomes for unit in outcome.units} == {
        ALICE_SPACE,
        PROJECT_SPACE,
    }


def test_the_coordinates_key_is_untouched_where_no_routing_table_is_assembled() -> None:
    """未装配判定表的部署里内核完全不介入 ``coords`` 键，行为与本特性之前逐字一致。

    介入的后果是静默吞掉：坐标不产生落点，条目却照常写入，调用方既拿不到坐标生效的证据
    也收不到提示。不介入则该键留在参数袋里，照旧由标量校验按嵌套字典拒绝——那正是本特性
    之前的行为。检索侧同理，留在 ``extensions`` 里透传给自定义检索模块。
    """
    api = _kernel(with_router=False).api
    with pytest.raises(ValidationError, match="仅支持 JSON 标量或字符串数组"):
        api.add("内容", Scope(org=ORG, user="alice"), security=SEC_ALICE, system_metadata=COORDS_P1)


def test_a_non_scope_argument_is_a_validation_error_not_an_attribute_error(api) -> None:
    """``scope`` 传非 ``Scope`` 对象是参数校验失败，不是内核内部错误。

    分流判据要读 ``scope.space``，类型校验须先于它——否则非 ``Scope`` 入参在判据里撞上
    ``AttributeError``，而直写路径的同名校验排在判据之后、兜不到这条。
    """
    with pytest.raises(ValidationError, match="scope must be Scope"):
        api.add("内容", "u_alice", security=SEC_ALICE)


def test_a_middle_write_is_excluded_from_the_decision_path(api) -> None:
    """``middle=true`` 不判定：落点取入参 ``scope``，判定标签键照常补齐。

    该路径与 ``infer`` / ``procedural`` 的载体不同——后两者的原文进 ``message_store``、
    不参与检索，落 fallback 无影响；``middle`` 的原文按 ``tier=WORKING`` 直接建索引，走
    判定分流则落 fallback 且一个收窄维标签键都不落，随后在任何带 ``coords`` 的检索里被
    第二族谓词 ``IN ["", value]`` 静默排除。
    """
    units = api.add(
        "项目部署在集群 A",
        scope=Scope(org=ORG, space=PROJECT_SPACE), security=SEC_ALICE,
        system_metadata={"infer": "true", "middle": "true", **COORDS_P1},
    )
    # 落点是入参空间，不是判定给的 fallback。
    assert [unit.scope.space for unit in units] == [PROJECT_SPACE]
    assert not _units_in(api, ALICE_SPACE, ALICE)
    # 键恒存在（不变量 8）：取值一律空串——本路径不经判定，「这条属于哪个项目」无从得知。
    landed = units[0].system_metadata
    for key in ("project_id", "team_id", "session_id"):
        assert landed[key] == ""
    # 不判定即不产生判定产物：类别名不写。
    assert "memory_class" not in landed


def test_a_middle_write_without_a_landing_space_is_rejected(api) -> None:
    """``middle=true`` 不判定，因此落点必须由 ``scope.space`` 给出。

    与「判定表非空 + 无 coords 键 + space 为空」同一条拒绝：坐标不产生落点时，空 space
    既未指定落点也未请求判定。替换掉的是原本落 fallback 而调用方看不出差别的形态。
    """
    with pytest.raises(ValidationError, match="写入落点未声明"):
        api.add(
            "项目部署在集群 A",
            scope=Scope(org=ORG), security=SEC_ALICE,
            system_metadata={"infer": "true", "middle": "true", **COORDS_P1},
        )


def test_a_cross_space_search_checks_the_space_lifecycle_state(api) -> None:
    """跨空间检索与单空间路径同一口径地校验空间状态（F07「空间状态校验」）。

    不校验时调用方在 ``extensions["spaces"]`` 里点名一个正在清理的空间即可照常拿到内容，
    同一个 ``search`` 的两条路径对该空间给出两种结果。

    状态由探针注入而不由真实流转造出：``DELETING`` 只由删除流程内部置入，而管理面
    ``update`` 的流转白名单不接受它，本仓内没有可达该状态的公开路径。本例断言的是接线
    ——跨空间逐空间判权之后确实过一次状态校验，且该校验的拒绝按通道错误处置、不中止整
    次检索。状态判据本身的用例在 ``test_space_aware_authorization.py``。
    """
    api.add("项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1)
    api.add("偏好深色主题", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=NO_COORDS)

    checked: list[tuple[str, str]] = []
    original = api._ensure_space_state_allows

    def spy(target, action, entry, **kwargs):
        checked.append((target.space, entry))
        if target.space == PROJECT_SPACE:
            raise ValidationError(f"space is being deleted: {ORG}/{target.space}")
        return original(target, action, entry, **kwargs)

    api._ensure_space_state_allows = spy
    try:
        result = api.search(
            "深色主题",
            Context(scope=Scope(org=ORG), extensions={"spaces": [PROJECT_SPACE, ALICE_SPACE]}),
            security=SEC_ALICE,
            top_k=10,
        )
    finally:
        api._ensure_space_state_allows = original

    # 判权通过的每个候选空间各校验一次，入口名与单空间路径一致。
    assert checked == [(PROJECT_SPACE, "search"), (ALICE_SPACE, "search")]
    # 被拒的空间不贡献结果，且以 ChannelError 进 errors——「读不到」与「里面没内容」
    # 在返回值上必须可区分。
    assert [error.source for error in result.errors] == [PROJECT_SPACE]
    assert result.errors[0].error_type == "ValidationError"
    assert {item.content for item in result.items} == {"偏好深色主题"}


def test_the_write_candidate_set_truncates_by_render_order_not_by_write_permission() -> None:
    """截断按渲染顺序计，不按已通过写权的空间数计（F07「写入候选空间集合」）。

    判权要读空间事实并走判定链，是候选集计算唯一的外部调用，成本上界正是由这条截断给出。
    按写权结果计时，渲染出但不可写的空间不占名额，会全部被送去判权。
    """
    from jiuwen_memory.control.collective.write_targets import plan_write_targets

    naming = parse_route_table(ROUTE_TABLE).naming
    judged: list[str] = []

    def can_write(scope: Scope, _require_writable_state: bool) -> bool:
        judged.append(scope.space)
        return scope.space == ALICE_SPACE  # 只有 fallback 可写

    targets = plan_write_targets(
        ORG, {"project": "p1", "user": "alice"}, naming, can_write=can_write, limit=1
    )
    # 渲染出两个空间，上限 1：只判第一个（fallback），第二个不参与判权。
    assert judged == [ALICE_SPACE]
    assert [scope.space for scope in targets.candidates] == [ALICE_SPACE]


def test_an_item_level_entry_authorizes_its_second_segment_against_the_stored_scope(api) -> None:
    """``get`` / ``update`` 第二段的鉴权目标取条目真源 scope，不沿用入参（F07）。

    它是「判定第 8 步不会命中」的两个前置条件之一，与「回填后条目 scope 只有两维」各兜
    一重，两条的失效方向相反。沿用入参即把两重约束落在同一个取值上。四个条目级入口同一
    口径，故与 ``list`` / ``delete`` 一并断言。
    """
    unit = api.add(
        "项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1
    )[0]
    targets: list[tuple[str, str]] = []
    original = api._authorize

    def spy(identity, target, action, entry, *args, **kwargs):
        targets.append((entry, target.space))
        return original(identity, target, action, entry, *args, **kwargs)

    api._authorize = spy
    try:
        api.get(unit.id, Scope(org=ORG, space=PROJECT_SPACE), security=SEC_ALICE)
        api.update(
            unit.id,
            Scope(org=ORG, space=PROJECT_SPACE),
            MemoryPatch(content="项目部署在集群 B"),
            security=SEC_ALICE,
        )
    finally:
        api._authorize = original

    # 两段各一次，第二段的目标是条目真源所在空间。
    assert [space for entry, space in targets if entry == "get"] == [
        PROJECT_SPACE,
        unit.scope.space,
    ]
    assert [space for entry, space in targets if entry == "update"] == [
        PROJECT_SPACE,
        unit.scope.space,
    ]


def test_a_router_failure_is_recorded_in_the_audit_not_only_swallowed(api) -> None:
    """判定器故障不阻断写入，但要在审计里留下痕迹并带上可区分的原因。

    降级不抛异常、落点是 fallback、内容照常落盘，因此在调用方看来它与「判定器正常工作且
    判成了用户记忆」完全一样。不记这一条时，判定器整体故障可以持续很久而无人察觉。
    """

    def _boom(_units, _ctx):
        raise RuntimeError("model unavailable")

    original = api._router.route
    api._router.route = _boom
    try:
        api.add(
            "项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE,
            system_metadata=COORDS_P1,
        )
    finally:
        api._router.route = original

    events = [
        event
        for event in api._audit.query({"action": "add"})
        if event.detail.get("entry") == "routing_degraded"
    ]
    assert len(events) == 1
    assert events[0].detail["degraded"] == "1"
    assert events[0].detail["total"] == "1"
    assert "RuntimeError" in events[0].detail["reasons"]
    assert "model unavailable" in events[0].detail["reasons"]


def test_a_normal_decision_writes_no_degradation_record(api) -> None:
    """按判定原样落点的写入不产生降级记录——否则该记录在审计里没有分辨力。"""
    api.add(
        "项目部署在集群 A", scope=Scope(org=ORG), security=SEC_ALICE, system_metadata=COORDS_P1
    )

    degraded = [
        event
        for event in api._audit.query({"action": "add"})
        if event.detail.get("entry") == "routing_degraded"
    ]
    assert degraded == []
