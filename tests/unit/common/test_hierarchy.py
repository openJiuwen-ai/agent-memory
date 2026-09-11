# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""hierarchy: ref-level invariants, tree-level validation, and index projection."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    ChunkVector,
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    HierarchyStatus,
    MemoryUnit,
    Scope,
    Segment,
    hierarchy_index_metadata,
    validate_ref,
    validate_tree,
)
from tests.unit.common.fixtures import at, node, scope

pytestmark = pytest.mark.unit

# -- HierarchyRef 基础 ------------------------------------------------------ #


def test_default_ref_is_empty_structure() -> None:
    ref = HierarchyRef()

    assert ref.is_empty, "kind 与 role 同时缺省即空结构"
    assert ref.status is HierarchyStatus.ACTIVE, "status 必填且默认 ACTIVE"


def test_memory_unit_defaults_to_empty_hierarchy() -> None:
    unit = MemoryUnit(id="u1", segments=[Segment(content="普通记忆")])

    assert unit.hierarchy.is_empty, "未挂树的记忆必须读为空结构"


def test_memory_unit_keeps_existing_positional_fields() -> None:
    original = MemoryUnit(
        id="u1", entities=["entity"], vectors=[ChunkVector(id="u1-0", seq=0, vector=[0.1])]
    )
    legacy_values = [
        getattr(original, entry.name) for entry in fields(original) if entry.name != "hierarchy"
    ]

    rebuilt = MemoryUnit(*legacy_values)

    assert rebuilt.entities == original.entities, "新增结构字段不能移动既有位置参数"
    assert rebuilt.vectors == original.vectors, "既有 vectors 位置参数必须保留"
    assert rebuilt.hierarchy.is_empty, "旧位置参数调用应得到默认空结构"


def test_memory_units_do_not_share_default_hierarchy() -> None:
    first, second = MemoryUnit(id="a"), MemoryUnit(id="b")

    first.hierarchy.child_ids.append("child")

    assert second.hierarchy.child_ids == [], "每条记忆必须持有独立的默认结构对象"


def test_child_scope_and_parent_scope_fall_back_to_owner_scope() -> None:
    owner = scope(session="s1")
    ref = HierarchyRef(
        kind=HierarchyKind.TIME,
        role=HierarchyRole.SNAPSHOT,
        child_ids=["c1"],
        span_start=at(0),
        span_end=at(0),
    )

    assert ref.child_scope_at(0, owner) == owner, "child_scopes 为空时退化为 owner 的 Scope"
    assert ref.resolved_parent_scope(owner) == owner, "parent_scope 为空时退化为 owner 的 Scope"


def test_explicit_child_scope_overrides_owner_scope() -> None:
    owner, child = scope(), scope(agent="a1", session="s1")
    ref = HierarchyRef(
        kind=HierarchyKind.TIME,
        role=HierarchyRole.TIME_SPAN,
        child_ids=["c1"],
        child_scopes=[child],
        span_start=at(0),
        span_end=at(1),
    )

    assert ref.child_scope_at(0, owner) == child, "显式 child_scopes 必须优先于 owner"


# -- validate_ref ----------------------------------------------------------- #


def test_validate_ref_accepts_empty_structure() -> None:
    validate_ref(HierarchyRef())


@pytest.mark.parametrize(
    "ref",
    [
        HierarchyRef(kind=HierarchyKind.TIME),
        HierarchyRef(role=HierarchyRole.SNAPSHOT),
    ],
    ids=["only_kind", "only_role"],
)
def test_validate_ref_rejects_half_declared_identity(ref: HierarchyRef) -> None:
    with pytest.raises(ValidationError, match="同时设置或同时缺省"):
        validate_ref(ref)


@pytest.mark.parametrize(
    "ref",
    [
        HierarchyRef(parent_id="p"),
        HierarchyRef(child_ids=["c"]),
        HierarchyRef(parent_scope=scope()),
    ],
    ids=["parent_id", "child_ids", "parent_scope"],
)
def test_validate_ref_rejects_edges_on_empty_structure(ref: HierarchyRef) -> None:
    with pytest.raises(ValidationError, match="空 hierarchy 不得携带父子引用"):
        validate_ref(ref)


def test_validate_ref_rejects_span_on_empty_structure() -> None:
    with pytest.raises(ValidationError, match="空 hierarchy 不得携带区间"):
        validate_ref(HierarchyRef(span_start=at(0), span_end=at(1)))


def test_validate_ref_rejects_half_declared_span() -> None:
    ref = HierarchyRef(kind=HierarchyKind.TOPIC, role=HierarchyRole.NODE, span_start=at(0))

    with pytest.raises(ValidationError, match="区间必须成对出现"):
        validate_ref(ref)


def test_validate_ref_rejects_reversed_span() -> None:
    ref = HierarchyRef(
        kind=HierarchyKind.TIME,
        role=HierarchyRole.SNAPSHOT,
        span_start=at(2),
        span_end=at(1),
    )

    with pytest.raises(ValidationError, match="span_start 不得晚于 span_end"):
        validate_ref(ref)


def test_validate_ref_requires_span_for_time_kind() -> None:
    ref = HierarchyRef(kind=HierarchyKind.TIME, role=HierarchyRole.SNAPSHOT)

    with pytest.raises(ValidationError, match="TIME 的所有节点必须有区间"):
        validate_ref(ref)


def test_validate_ref_allows_missing_span_for_non_time_kind() -> None:
    validate_ref(HierarchyRef(kind=HierarchyKind.TOPIC, role=HierarchyRole.NODE))


def test_validate_ref_rejects_child_scopes_length_mismatch() -> None:
    ref = HierarchyRef(
        kind=HierarchyKind.TOPIC,
        role=HierarchyRole.NODE,
        child_ids=["a", "b"],
        child_scopes=[scope()],
    )

    with pytest.raises(ValidationError, match="必须与 child_ids 等长"):
        validate_ref(ref)


def test_validate_ref_rejects_duplicate_children() -> None:
    ref = HierarchyRef(kind=HierarchyKind.TOPIC, role=HierarchyRole.NODE, child_ids=["a", "a"])

    with pytest.raises(ValidationError, match="child_ids 不得重复"):
        validate_ref(ref)


def test_validate_ref_rejects_self_reference() -> None:
    ref = HierarchyRef(kind=HierarchyKind.TOPIC, role=HierarchyRole.NODE, parent_id="me")

    with pytest.raises(ValidationError, match="parent_id 不得自指"):
        validate_ref(ref, unit_id="me")


def test_validate_ref_rejects_self_in_children() -> None:
    ref = HierarchyRef(kind=HierarchyKind.TOPIC, role=HierarchyRole.NODE, child_ids=["me"])

    with pytest.raises(ValidationError, match="child_ids 不得包含自身"):
        validate_ref(ref, unit_id="me")


# -- validate_tree ---------------------------------------------------------- #


def test_validate_tree_accepts_empty_input() -> None:
    validate_tree([])
    validate_tree([MemoryUnit(id="plain", segments=[Segment(content="无结构")])])


def test_validate_tree_accepts_cross_session_tree() -> None:
    """决策 2b 的核心场景：父在 tree home scope，叶驻留各自 session。"""
    morning, afternoon = scope(agent="a1", session="s_am"), scope(agent="a1", session="s_pm")
    units = [
        node(
            "parent",
            HierarchyRole.TIME_SPAN,
            (at(0), at(7)),
            ref=HierarchyRef(child_ids=["leaf_am", "leaf_pm"], child_scopes=[morning, afternoon]),
        ),
        node(
            "leaf_am",
            HierarchyRole.SNAPSHOT,
            (at(0), at(2)),
            unit_scope=morning,
            ref=HierarchyRef(parent_id="parent", parent_scope=scope()),
        ),
        node(
            "leaf_pm",
            HierarchyRole.SNAPSHOT,
            (at(5), at(7)),
            unit_scope=afternoon,
            ref=HierarchyRef(parent_id="parent", parent_scope=scope()),
        ),
    ]

    validate_tree(units)


def test_validate_tree_allows_parent_at_coarser_agent_level() -> None:
    """父清空 agent、子带 agent 只是归属层级更粗，不是跨主体——回归用例。"""
    child_scope = scope(agent="a1", session="s1")
    units = [
        node(
            "parent",
            HierarchyRole.TIME_SPAN,
            (at(0), at(2)),
            ref=HierarchyRef(child_ids=["leaf"], child_scopes=[child_scope]),
        ),
        node(
            "leaf",
            HierarchyRole.SNAPSHOT,
            (at(0), at(1)),
            unit_scope=child_scope,
            ref=HierarchyRef(parent_id="parent", parent_scope=scope()),
        ),
    ]

    validate_tree(units)


def test_validate_tree_rejects_self_cycle() -> None:
    with pytest.raises(ValidationError, match="不得自指"):
        validate_tree(
            [node("a", HierarchyRole.TIME_SPAN, (at(0), at(1)), ref=HierarchyRef(parent_id="a"))]
        )


def test_validate_tree_rejects_ancestor_cycle() -> None:
    first = node(
        "a",
        HierarchyRole.TIME_SPAN,
        (at(0), at(2)),
        ref=HierarchyRef(parent_id="b", child_ids=["b"]),
    )
    second = node(
        "b",
        HierarchyRole.TIME_SPAN,
        (at(0), at(2)),
        ref=HierarchyRef(parent_id="a", child_ids=["a"]),
    )

    with pytest.raises(ValidationError, match="成环"):
        validate_tree([first, second])


@pytest.mark.parametrize("dimension", ["org", "space"], ids=["org", "space"])
def test_validate_tree_rejects_hard_boundary_crossing(dimension: str) -> None:
    other = Scope(**{**scope().__dict__, dimension: "other"})
    units = [
        node(
            "parent",
            HierarchyRole.TIME_SPAN,
            (at(0), at(2)),
            ref=HierarchyRef(child_ids=["leaf"], child_scopes=[other]),
        ),
        node(
            "leaf",
            HierarchyRole.SNAPSHOT,
            (at(0), at(1)),
            unit_scope=other,
            ref=HierarchyRef(parent_id="parent", parent_scope=scope()),
        ),
    ]

    with pytest.raises(ValidationError, match="不得跨 org/space"):
        validate_tree(units)


@pytest.mark.parametrize(
    ("first_scope", "second_scope", "dimension"),
    [
        (scope(), scope(user="lisi"), "user"),
        (scope(agent="a1"), scope(agent="a2"), "agent"),
    ],
    ids=["user", "agent"],
)
def test_validate_tree_rejects_cross_principal_by_default(
    first_scope: Scope, second_scope: Scope, dimension: str
) -> None:
    """两侧都非空且不同才算跨主体——一侧为空只是归属层级更粗（见上一用例）。"""
    first = node("a", HierarchyRole.SNAPSHOT, (at(0), at(1)), unit_scope=first_scope)
    second = node("b", HierarchyRole.SNAPSHOT, (at(0), at(1)), unit_scope=second_scope)

    with pytest.raises(ValidationError, match=f"未开启跨 {dimension} 建树"):
        validate_tree([first, second])


def test_validate_tree_allows_cross_user_when_explicitly_enabled() -> None:
    units = [
        node("a", HierarchyRole.SNAPSHOT, (at(0), at(1))),
        node("b", HierarchyRole.SNAPSHOT, (at(0), at(1)), unit_scope=scope(user="lisi")),
    ]

    validate_tree(units, allow_cross_user=True)


def test_validate_tree_rejects_multiple_parents_in_same_kind() -> None:
    units = [
        node("p1", HierarchyRole.TIME_SPAN, (at(0), at(2)), ref=HierarchyRef(child_ids=["c"])),
        node("p2", HierarchyRole.TIME_SPAN, (at(0), at(2)), ref=HierarchyRef(child_ids=["c"])),
        node("c", HierarchyRole.SNAPSHOT, (at(0), at(1)), ref=HierarchyRef(parent_id="p1")),
    ]

    with pytest.raises(ValidationError, match="多父"):
        validate_tree(units)


def test_validate_tree_rejects_child_not_acknowledging_parent() -> None:
    units = [
        node("p", HierarchyRole.TIME_SPAN, (at(0), at(2)), ref=HierarchyRef(child_ids=["c"])),
        node(
            "c", HierarchyRole.SNAPSHOT, (at(0), at(1)), ref=HierarchyRef(parent_id="someone_else")
        ),
    ]

    with pytest.raises(ValidationError, match="双向边不一致"):
        validate_tree(units)


def test_validate_tree_rejects_parent_not_listing_child() -> None:
    units = [
        node("p", HierarchyRole.TIME_SPAN, (at(0), at(2))),
        node("c", HierarchyRole.SNAPSHOT, (at(0), at(1)), ref=HierarchyRef(parent_id="p")),
    ]

    with pytest.raises(ValidationError, match="双向边不一致"):
        validate_tree(units)


def test_validate_tree_rejects_parent_span_not_covering_child() -> None:
    units = [
        node("p", HierarchyRole.TIME_SPAN, (at(1), at(2)), ref=HierarchyRef(child_ids=["c"])),
        node("c", HierarchyRole.SNAPSHOT, (at(0), at(3)), ref=HierarchyRef(parent_id="p")),
    ]

    with pytest.raises(ValidationError, match="未覆盖子"):
        validate_tree(units)


def test_validate_tree_rejects_mixed_kinds() -> None:
    units = [
        node("a", HierarchyRole.SNAPSHOT, (at(0), at(1))),
        node("b", HierarchyRole.NODE, ref=HierarchyRef(kind=HierarchyKind.TOPIC)),
    ]

    with pytest.raises(ValidationError, match="必须同 kind"):
        validate_tree(units)


def test_validate_tree_rejects_duplicate_nodes() -> None:
    units = [
        node("dup", HierarchyRole.SNAPSHOT, (at(0), at(1))),
        node("dup", HierarchyRole.SNAPSHOT, (at(0), at(1))),
    ]

    with pytest.raises(ValidationError, match="重复节点"):
        validate_tree(units)


def test_validate_tree_skips_references_outside_the_candidate_set() -> None:
    """集合外引用无从核对，跳过而不误报——Composer 只校验自己提交的候选子树。"""
    units = [
        node("p", HierarchyRole.TIME_SPAN, (at(0), at(2)), ref=HierarchyRef(child_ids=["absent"])),
        node(
            "c", HierarchyRole.SNAPSHOT, (at(0), at(1)), ref=HierarchyRef(parent_id="absent_parent")
        ),
    ]

    validate_tree(units)


def test_validate_ref_accepts_equal_child_ids_in_distinct_scopes() -> None:
    ref = HierarchyRef(
        kind=HierarchyKind.TOPIC,
        role=HierarchyRole.NODE,
        child_ids=["c", "c"],
        child_scopes=[scope(session="s1"), scope(session="s2")],
    )

    validate_ref(ref)


def test_validate_ref_rejects_duplicate_explicit_child_keys() -> None:
    ref = HierarchyRef(
        kind=HierarchyKind.TOPIC,
        role=HierarchyRole.NODE,
        child_ids=["c", "c"],
        child_scopes=[scope(session="s1"), scope(session="s1")],
    )

    with pytest.raises(ValidationError, match="child_ids 不得重复"):
        validate_ref(ref)


def test_validate_tree_accepts_same_parent_and_child_id_in_distinct_scopes() -> None:
    owner, child_scope = scope(), scope(session="s1")
    parent = node(
        "same",
        HierarchyRole.TIME_SPAN,
        (at(0), at(2)),
        ref=HierarchyRef(child_ids=["same"], child_scopes=[child_scope]),
    )
    child = node(
        "same",
        HierarchyRole.SNAPSHOT,
        (at(0), at(1)),
        unit_scope=child_scope,
        ref=HierarchyRef(parent_id="same", parent_scope=owner),
    )

    validate_tree([parent, child])


def test_validate_tree_accepts_same_child_id_in_distinct_scopes() -> None:
    first_scope, second_scope = scope(session="s1"), scope(session="s2")
    parent = node(
        "p",
        HierarchyRole.TIME_SPAN,
        (at(0), at(2)),
        ref=HierarchyRef(child_ids=["c", "c"], child_scopes=[first_scope, second_scope]),
    )
    children = [
        node(
            "c",
            HierarchyRole.SNAPSHOT,
            (at(0), at(1)),
            unit_scope=child_scope,
            ref=HierarchyRef(parent_id="p", parent_scope=scope()),
        )
        for child_scope in (first_scope, second_scope)
    ]

    validate_tree([parent, *children])


@pytest.mark.parametrize("direction", ["parent", "child"])
def test_validate_tree_rejects_explicit_same_scope_self_reference(direction: str) -> None:
    if direction == "parent":
        ref = HierarchyRef(parent_id="self", parent_scope=scope())
    else:
        ref = HierarchyRef(child_ids=["self"], child_scopes=[scope()])
    unit = node("self", HierarchyRole.TIME_SPAN, (at(0), at(1)), ref=ref)

    with pytest.raises(ValidationError, match="不得自指|不得包含自身"):
        validate_tree([unit])


def test_validate_tree_rejects_parent_listing_child_at_wrong_scope() -> None:
    parent = node(
        "p",
        HierarchyRole.TIME_SPAN,
        (at(0), at(2)),
        ref=HierarchyRef(child_ids=["c"], child_scopes=[scope(session="absent")]),
    )
    child = node(
        "c",
        HierarchyRole.SNAPSHOT,
        (at(0), at(1)),
        unit_scope=scope(session="present"),
        ref=HierarchyRef(parent_id="p", parent_scope=scope()),
    )

    with pytest.raises(ValidationError, match="双向边不一致"):
        validate_tree([parent, child])


def test_validate_tree_rejects_distinct_same_id_parents_of_external_child() -> None:
    parents = [
        node(
            "p",
            HierarchyRole.TIME_SPAN,
            (at(0), at(2)),
            unit_scope=scope(session=session),
            ref=HierarchyRef(child_ids=["absent"], child_scopes=[scope()]),
        )
        for session in ("s1", "s2")
    ]

    with pytest.raises(ValidationError, match="多父"):
        validate_tree(parents)


@pytest.mark.parametrize("direction", ["parent", "child"])
@pytest.mark.parametrize(
    ("dimension", "allow_cross_user", "message"),
    [
        ("org", False, "不得跨 org/space"),
        ("space", False, "不得跨 org/space"),
        ("org", True, "不得跨 org/space"),
        ("space", True, "不得跨 org/space"),
        ("user", False, "未开启跨 user"),
        ("agent", False, "未开启跨 agent"),
    ],
)
def test_validate_tree_checks_boundary_of_external_reference(
    direction: str, dimension: str, allow_cross_user: bool, message: str
) -> None:
    owner = scope(agent="a1")
    target = replace(owner, **{dimension: "other"})
    if direction == "parent":
        ref = HierarchyRef(parent_id="absent", parent_scope=target)
    else:
        ref = HierarchyRef(child_ids=["absent"], child_scopes=[target])
    unit = node("u", HierarchyRole.TIME_SPAN, (at(0), at(1)), unit_scope=owner, ref=ref)

    with pytest.raises(ValidationError, match=message):
        validate_tree([unit], allow_cross_user=allow_cross_user)


@pytest.mark.parametrize("dimension", ["user", "agent"])
@pytest.mark.parametrize("direction", ["parent", "child"])
def test_validate_tree_allows_external_principal_with_explicit_permission(
    dimension: str, direction: str
) -> None:
    owner = scope(agent="a1")
    target = replace(owner, **{dimension: "other"})
    if direction == "parent":
        ref = HierarchyRef(parent_id="absent", parent_scope=target)
    else:
        ref = HierarchyRef(child_ids=["absent"], child_scopes=[target])
    unit = node("u", HierarchyRole.TIME_SPAN, (at(0), at(1)), unit_scope=owner, ref=ref)

    validate_tree([unit], allow_cross_user=True)


def test_validate_tree_skips_valid_external_cross_session_references() -> None:
    external = scope(session="outside")
    unit = node(
        "u",
        HierarchyRole.TIME_SPAN,
        (at(0), at(1)),
        ref=HierarchyRef(
            parent_id="absent_parent",
            parent_scope=external,
            child_ids=["absent_child"],
            child_scopes=[external],
        ),
    )

    validate_tree([unit])


@pytest.mark.parametrize(
    "ref",
    [
        HierarchyRef(parent_id="p"),
        HierarchyRef(child_ids=["c"]),
        HierarchyRef(parent_scope=scope()),
        HierarchyRef(span_start=at(0), span_end=at(1)),
    ],
    ids=["parent", "children", "parent_scope", "span"],
)
def test_validate_tree_rejects_invalid_empty_structure(ref: HierarchyRef) -> None:
    unit = MemoryUnit(id="empty", scope=scope(), hierarchy=ref)

    with pytest.raises(ValidationError, match="空 hierarchy 不得携带"):
        validate_tree([unit])


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (at(0).replace(tzinfo=None), at(1)),
        (at(0), at(1).replace(tzinfo=None)),
        (
            at(0).replace(tzinfo=None),
            at(0).astimezone(timezone(timedelta(hours=8))),
        ),
    ],
    ids=["naive_start", "naive_end", "same_instant_with_offset"],
)
def test_validate_ref_compares_mixed_datetimes_as_utc(start: datetime, end: datetime) -> None:
    ref = HierarchyRef(
        kind=HierarchyKind.TIME, role=HierarchyRole.SNAPSHOT, span_start=start, span_end=end
    )

    validate_ref(ref)


@pytest.mark.parametrize("naive_start", [True, False])
def test_validate_ref_rejects_submillisecond_reversed_mixed_span(naive_start: bool) -> None:
    start = at(0) + timedelta(microseconds=1)
    end = at(0)
    if naive_start:
        start = start.replace(tzinfo=None)
    else:
        end = end.replace(tzinfo=None)
    ref = HierarchyRef(
        kind=HierarchyKind.TIME, role=HierarchyRole.SNAPSHOT, span_start=start, span_end=end
    )

    with pytest.raises(ValidationError, match="span_start 不得晚于 span_end"):
        validate_ref(ref)


@pytest.mark.parametrize("naive_parent", [True, False])
def test_validate_tree_accepts_mixed_datetime_coverage_at_equal_boundary(
    naive_parent: bool,
) -> None:
    naive_span = (at(0).replace(tzinfo=None), at(1).replace(tzinfo=None))
    offset = timezone(timedelta(hours=8))
    aware_span = (at(0).astimezone(offset), at(1).astimezone(offset))
    parent_span, child_span = (naive_span, aware_span) if naive_parent else (aware_span, naive_span)
    parent = node("p", HierarchyRole.TIME_SPAN, parent_span, ref=HierarchyRef(child_ids=["c"]))
    child = node("c", HierarchyRole.SNAPSHOT, child_span, ref=HierarchyRef(parent_id="p"))

    validate_tree([parent, child])


@pytest.mark.parametrize("naive_parent", [True, False])
@pytest.mark.parametrize("outside_edge", ["start", "end"])
def test_validate_tree_rejects_submillisecond_mixed_datetime_coverage_gap(
    naive_parent: bool, outside_edge: str
) -> None:
    parent_span = (at(0), at(1))
    child_start = at(0) - timedelta(microseconds=1) if outside_edge == "start" else at(0)
    child_end = at(1) + timedelta(microseconds=1) if outside_edge == "end" else at(1)
    child_span = (child_start, child_end)
    if naive_parent:
        parent_span = tuple(value.replace(tzinfo=None) for value in parent_span)
    else:
        child_span = tuple(value.replace(tzinfo=None) for value in child_span)
    parent = node("p", HierarchyRole.TIME_SPAN, parent_span, ref=HierarchyRef(child_ids=["c"]))
    child = node("c", HierarchyRole.SNAPSHOT, child_span, ref=HierarchyRef(parent_id="p"))

    with pytest.raises(ValidationError, match="未覆盖子"):
        validate_tree([parent, child])


# -- hierarchy_index_metadata ----------------------------------------------- #


def test_index_metadata_is_empty_for_empty_structure() -> None:
    assert hierarchy_index_metadata(HierarchyRef()) == {}, "空结构不得污染索引投影"


def test_index_metadata_projects_six_keys() -> None:
    ref = HierarchyRef(
        kind=HierarchyKind.TIME,
        role=HierarchyRole.TIME_SPAN,
        parent_id="scene-1",
        span_start=at(0),
        span_end=at(2),
    )

    assert hierarchy_index_metadata(ref) == {
        "hierarchy_kind": "time",
        "hierarchy_role": "time_span",
        "hierarchy_status": "active",
        "parent_id": "scene-1",
        # epoch 毫秒而非 ISO 文本：FilterClause 的范围算子只接受有限数值，
        # 字符串做 LTE/GTE 会被校验拒绝，各召回路静默降级为空。
        "span_start": int(at(0).timestamp() * 1000),
        "span_end": int(at(2).timestamp() * 1000),
    }


def test_index_metadata_always_writes_status() -> None:
    ref = HierarchyRef(
        kind=HierarchyKind.TOPIC,
        role=HierarchyRole.NODE,
        status=HierarchyStatus.DISMISSED,
    )
    projected = hierarchy_index_metadata(ref)

    assert projected["hierarchy_status"] == "dismissed"
    assert "span_start" not in projected, "未声明区间时不投影 span"


def test_index_metadata_normalises_span_to_utc() -> None:
    """带偏移的时间换算到 UTC——同一索引内的区间必须可直接比较。"""
    eastern_eight = timezone(timedelta(hours=8))
    ref = HierarchyRef(
        kind=HierarchyKind.TIME,
        role=HierarchyRole.SNAPSHOT,
        span_start=datetime(2026, 8, 18, 17, 0, tzinfo=eastern_eight),
        span_end=datetime(2026, 8, 18, 17, 0, tzinfo=eastern_eight),
    )

    assert hierarchy_index_metadata(ref)["span_start"] == int(at(0).timestamp() * 1000)


def test_index_metadata_reads_naive_span_as_utc() -> None:
    ref = HierarchyRef(
        kind=HierarchyKind.TIME,
        role=HierarchyRole.SNAPSHOT,
        span_start=datetime(2026, 8, 18, 9, 0),
        span_end=datetime(2026, 8, 18, 9, 0),
    )

    assert hierarchy_index_metadata(ref)["span_end"] == int(at(0).timestamp() * 1000)


def test_index_metadata_span_is_numeric_for_range_operators() -> None:
    """区间必须是数值——FilterClause 的 GT/GTE/LT/LTE 只接受有限数值。"""
    from jiuwen_memory.common.type_def import FilterClause, FilterOp

    ref = HierarchyRef(
        kind=HierarchyKind.TIME,
        role=HierarchyRole.SNAPSHOT,
        span_start=at(0),
        span_end=at(1),
    )
    projected = hierarchy_index_metadata(ref)

    assert isinstance(projected["span_start"], int)
    # 构造范围谓词不抛错即证明取值类型合法（字符串会被 normalize 拒绝）
    FilterClause("span_start", FilterOp.LTE, projected["span_start"])
