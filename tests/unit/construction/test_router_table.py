"""判定表解析、加载期十二条校验与两个落盘不变量（S09「判定表」「归属判定算子」）。

本文件测的是纯函数：判定表解析的失效方向多为放行或静默收窄，而两者在集成用例里都不表现
为报错——加载期校验漏一条，症状要到线上某一类内容落错空间时才显现。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.construction.router import (
    EMPTY_ROUTE_TABLE,
    RouteContext,
    RouteDecision,
    Router,
    RouteTable,
    apply_decisions,
    build_decision,
    enforce_sanitized,
    parse_route_table,
    route_batch,
    with_all_tag_keys,
)

pytestmark = pytest.mark.unit

ORG = "acme"


def _table_config(**overrides):
    config = {
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
    config.update(overrides)
    return config


def _table() -> RouteTable:
    return parse_route_table(_table_config())


def _ctx(table: RouteTable, **coords) -> RouteContext:
    resolved = {"user": "alice", "agent": "a1", "session": "s1", **coords}
    spaces = table.naming.spaces(resolved)
    return RouteContext(
        coords=resolved,
        candidates=tuple(Scope(org=ORG, space=space) for space in spaces.values()),
        fallback=Scope(org=ORG, space=table.naming.fallback_space(resolved)),
        classes=table.classes,
        narrow_dims=table.narrow_dims,
    )


def _unit(content: str) -> MemoryUnit:
    return MemoryUnit(segments=[Segment(content=content)])


class _FixedRouter(Router):
    """按预置结果作答的判定实现，用来测调用处的兜底而不是模型行为。"""

    def __init__(self, decisions, table: RouteTable | None = None) -> None:
        self._decisions = decisions
        self._table = table or EMPTY_ROUTE_TABLE

    @property
    def table(self) -> RouteTable:
        return self._table

    def operator_type(self):
        from jiuwen_memory.construction.base import OperatorType

        return OperatorType.ROUTER

    def health(self) -> None:
        return None

    def route(self, units, ctx):
        if isinstance(self._decisions, Exception):
            raise self._decisions
        return self._decisions


# -- 解析产物 ------------------------------------------------------------- #


def test_an_absent_namespace_parses_into_the_empty_table() -> None:
    """未声明判定表即空表：写入侧 scope 必填、判定路径不可达，可灰度上线的前提。"""
    assert parse_route_table(None) is EMPTY_ROUTE_TABLE
    assert EMPTY_ROUTE_TABLE.is_empty()
    assert EMPTY_ROUTE_TABLE.tag_keys == frozenset()


def test_the_four_parse_products_each_land() -> None:
    table = _table()
    assert [item.name for item in table.classes] == [
        "user_memory",
        "project_memory",
        "team_memory",
    ]
    # 判定标签键 = 收窄维标签键 ∪ 记录维生成的键；记录维类别未声明 tag_key 时按 owner 生成。
    assert table.tag_keys == {"agent_id", "session_id", "project_id", "team_id"}
    assert table.naming.fallback_class == "user_memory"
    # 内核三项排在部署声明项之前，且部署声明项保序。
    assert table.coord_keys == ("user", "agent", "session", "project", "team")


def test_a_class_whose_coordinate_is_absent_does_not_render_a_space() -> None:
    """坐标缺项是常态：该类别不进候选集，而不是整次写入失败。"""
    table = _table()
    assert table.naming.spaces({"user": "alice"}) == {"user_memory": "u_alice"}
    assert table.naming.render("project_memory", {"user": "alice"}) == ""


def test_a_record_only_class_has_no_space_template() -> None:
    """记录维类别不落独立空间，只出一个标签键。"""
    table = _table()
    assert table.naming.template_for("team_memory") == ""
    assert "team_id" in table.tag_keys


# -- 加载期十二条校验 ------------------------------------------------------ #


@pytest.mark.parametrize(
    ("case", "config", "expected"),
    [
        (
            1,
            _table_config(
                memory_classes=[
                    {
                        "name": "user_memory",
                        "owner": "user",
                        "space_template": "u_{user}",
                        "fallback": True,
                        "cross_user": True,
                        "members": "everyone",
                    }
                ]
            ),
            "不得声明 cross_user",
        ),
        (
            2,
            _table_config(
                memory_classes=[
                    {
                        "name": "user_memory",
                        "owner": "user",
                        "space_template": "u_{user}",
                        "fallback": True,
                    },
                    {
                        "name": "project_memory",
                        "owner": "project",
                        "space_template": "p_{project}",
                        "cross_user": True,
                    },
                ]
            ),
            "须给出 members 或 sanitized",
        ),
        (
            3,
            _table_config(narrow_dims=[{"entity": "unknown", "tag_key": "x_id"}]),
            "不在坐标键集合内",
        ),
        (
            4,
            _table_config(
                memory_classes=[
                    {"name": "a", "owner": "user", "space_template": "u_{user}"},
                    {"name": "b", "owner": "project", "space_template": "p_{project}"},
                ]
            ),
            "fallback 类别须有且仅有一个",
        ),
        (
            5,
            _table_config(
                memory_classes=[
                    {
                        "name": "shared",
                        "owner": "project",
                        "space_template": "p_{project}",
                        "fallback": True,
                        "cross_user": True,
                        "members": "participants",
                    }
                ]
            ),
            "不得 cross_user",
        ),
        (
            6,
            _table_config(
                narrow_dims=[
                    {"entity": "agent", "tag_key": "agent_id", "applies_to": ["nope"]}
                ]
            ),
            "applies_to 引用了未声明的类别",
        ),
        (
            7,
            _table_config(narrow_dims=[{"entity": "team", "tag_key": "team_id"}]),
            "与记录维生成的键同名",
        ),
        (
            8,
            _table_config(narrow_dims=[{"entity": "agent", "tag_key": "tags"}]),
            "与内核系统元数据键同名",
        ),
        (
            9,
            _table_config(
                memory_classes=[
                    {
                        "name": "user_memory",
                        "owner": "user",
                        "space_template": "u_{user}_{tenant}",
                        "fallback": True,
                    }
                ]
            ),
            "引用了未声明的坐标键",
        ),
        (
            10,
            _table_config(
                memory_classes=[
                    {
                        "name": "user_memory",
                        "owner": "user",
                        "space_template": "u_{session}",
                        "fallback": True,
                    }
                ]
            ),
            "必须引用其 owner",
        ),
        (
            11,
            _table_config(
                memory_classes=[
                    {
                        "name": "user_memory",
                        "owner": "user",
                        "space_template": "u_{user}",
                        "fallback": True,
                    },
                    {"name": "twin", "owner": "user", "space_template": "u_{user}"},
                ]
            ),
            "space_template 相同",
        ),
        (
            12,
            _table_config(coord_entities=["user", "project"]),
            "不得声明内核自带坐标",
        ),
    ],
)
def test_each_load_time_check_fails_the_assembly(case: int, config, expected: str) -> None:
    """任一条不过即装配失败，且失败信息指出的是这一条。"""
    with pytest.raises(ValidationError, match=expected):
        parse_route_table(config)
    assert case  # 编号只作用例可读性，断言在上一行


def test_the_kernel_coordinate_check_runs_before_the_union() -> None:
    """第 12 条在求并之前校验：求并之后交集恒为空，判不出来。"""
    with pytest.raises(ValidationError, match="不得声明内核自带坐标"):
        parse_route_table(_table_config(coord_entities=["session"]))


# -- 两个落盘不变量 -------------------------------------------------------- #


def test_all_tag_keys_are_written_even_when_the_dimension_did_not_fire() -> None:
    """键恒存在是两族谓词语义可预期的前提：集合谓词在字段缺失时判为不匹配。"""
    table = _table()
    ctx = _ctx(table)
    filled = with_all_tag_keys({"agent_id": "a1"}, ctx)
    assert filled == {"agent_id": "a1", "session_id": "", "project_id": "", "team_id": ""}


def test_a_sanitized_class_falls_back_when_the_content_carries_a_principal_id() -> None:
    """脱敏声明约束的是落点：命中即改落 fallback，不改写内容也不阻断整批。"""
    config = _table_config(
        memory_classes=[
            {
                "name": "user_memory",
                "owner": "user",
                "space_template": "u_{user}",
                "fallback": True,
            },
            {
                "name": "skill",
                "owner": "project",
                "space_template": "p_{project}",
                "cross_user": True,
                "sanitized": True,
            },
        ]
    )
    table = parse_route_table(config)
    ctx = _ctx(table, project="p1")
    decision = RouteDecision(
        unit=_unit("alice 的部署环境是 K8s"),
        scope=Scope(org=ORG, space="p_p1"),
        memory_class="skill",
    )
    trimmed = enforce_sanitized(decision, ctx)
    assert trimmed.scope.space == "u_alice"
    assert trimmed.memory_class == "user_memory"
    # 内容不被改写：脱敏检查改的是落点，不是条目本身。
    assert trimmed.unit.content == "alice 的部署环境是 K8s"


def test_a_clean_content_stays_in_the_sanitized_class() -> None:
    table = parse_route_table(
        _table_config(
            memory_classes=[
                {
                    "name": "user_memory",
                    "owner": "user",
                    "space_template": "u_{user}",
                    "fallback": True,
                },
                {
                    "name": "skill",
                    "owner": "project",
                    "space_template": "p_{project}",
                    "cross_user": True,
                    "sanitized": True,
                },
            ]
        )
    )
    ctx = _ctx(table, project="p1")
    decision = RouteDecision(
        unit=_unit("部署前先跑一遍 lint"),
        scope=Scope(org=ORG, space="p_p1"),
        memory_class="skill",
    )
    assert enforce_sanitized(decision, ctx).scope.space == "p_p1"


# -- 调用处的三条兜底 ------------------------------------------------------ #


def test_an_unassembled_router_sends_the_whole_batch_to_fallback() -> None:
    table = _table()
    ctx = _ctx(table, project="p1")
    decisions = route_batch(None, [_unit("x"), _unit("y")], ctx)
    assert [d.scope.space for d in decisions] == ["u_alice", "u_alice"]
    assert all(d.memory_class == "user_memory" for d in decisions)


def test_a_failing_router_does_not_block_the_write() -> None:
    table = _table()
    ctx = _ctx(table, project="p1")
    router = _FixedRouter(RuntimeError("model unavailable"))
    decisions = route_batch(router, [_unit("x")], ctx)
    assert decisions[0].scope.space == "u_alice"
    assert "router unavailable" in decisions[0].reason


def test_a_target_outside_the_candidate_set_is_pulled_back_to_fallback() -> None:
    """判定不得扩权这条不靠实现自觉：落点不在候选集内即改落 fallback。"""
    table = _table()
    ctx = _ctx(table, project="p1")
    router = _FixedRouter(
        [RouteDecision(scope=Scope(org=ORG, space="u_bob"), memory_class="user_memory")]
    )
    decisions = route_batch(router, [_unit("x")], ctx)
    assert decisions[0].scope.space == "u_alice"
    assert "outside the authorized candidate set" in decisions[0].reason


def test_a_short_decision_list_sends_the_whole_batch_to_fallback() -> None:
    table = _table()
    ctx = _ctx(table, project="p1")
    router = _FixedRouter([RouteDecision(scope=Scope(org=ORG, space="p_p1"))])
    decisions = route_batch(router, [_unit("x"), _unit("y")], ctx)
    assert [d.scope.space for d in decisions] == ["u_alice", "u_alice"]


# -- 落点解析与产物写回 ---------------------------------------------------- #


def test_a_record_only_hit_lands_in_fallback_but_keeps_its_class_name() -> None:
    """记录维类别命中：落点是 fallback，类别名仍记该类别——落点追溯要的正是这一对照。"""
    table = _table()
    ctx = _ctx(table, team="t1")
    decision = build_decision(_unit("这个 team 用同一套评审流程"), "team_memory", (), ctx)
    assert decision.scope.space == "u_alice"
    assert decision.memory_class == "team_memory"
    assert decision.tags["team_id"] == "t1"


def test_a_narrow_dimension_takes_its_value_from_the_coordinates() -> None:
    table = _table()
    ctx = _ctx(table, project="p1")
    decision = build_decision(_unit("x"), "project_memory", ("project_id",), ctx)
    assert decision.scope.space == "p_p1"
    assert decision.tags["project_id"] == "p1"


def test_applying_decisions_rewrites_scope_and_records_the_class() -> None:
    table = _table()
    ctx = _ctx(table, project="p1")
    decisions = route_batch(
        _FixedRouter(
            [
                RouteDecision(scope=Scope(org=ORG, space="p_p1"), memory_class="project_memory"),
                RouteDecision(discarded=True),
            ]
        ),
        [_unit("keep"), _unit("drop")],
        ctx,
    )
    kept = apply_decisions(decisions)
    assert [unit.content for unit in kept] == ["keep"]
    assert kept[0].scope.space == "p_p1"
    assert kept[0].system_metadata["memory_class"] == "project_memory"
    # 键恒存在：本条未命中的收窄维也写空串。
    assert kept[0].system_metadata["session_id"] == ""


def test_a_record_only_class_without_a_tag_key_or_owner_is_rejected() -> None:
    """校验 13：`record_only` 类别须能生成标签键。

    两者皆无时 `record_tag_key_of` 返回空串，该类别不产生任何标签键——命中它的条目只落
    fallback 空间、归属实体一点不记，声明整体失效且不报错。
    """
    with pytest.raises(ValidationError) as excinfo:
        parse_route_table(
            {
                "coord_entities": ["team"],
                "memory_classes": [
                    {
                        "name": "user_memory",
                        "owner": "user",
                        "space_template": "u_{user}",
                        "fallback": True,
                    },
                    {"name": "team_memory", "record_only": True},
                ],
            }
        )
    assert "tag_key" in str(excinfo.value)
