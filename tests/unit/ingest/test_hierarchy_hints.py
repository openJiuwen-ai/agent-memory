# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ingest hierarchy hints: reserved-key mapping and the six admission rules."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.normalizer.normalizer_impl.passthrough_normalizer import (
    PassthroughNormalizer,
)
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRole,
    HierarchyStatus,
    MetadataValueType,
    RawPayload,
    Scope,
)
from jiuwen_memory.ingest.hierarchy_hints import extract_hierarchy_hints
from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import SimpleIngestor

pytestmark = pytest.mark.unit

MOMENT = "2026-08-18T09:32:00+00:00"
LATER = "2026-08-18T11:40:00+00:00"


def time_leaf(**overrides: MetadataValueType) -> dict[str, MetadataValueType]:
    """一组合法的 TIME 叶提示，按需覆盖单个键。"""
    hints: dict[str, MetadataValueType] = {
        "hierarchy_kind": "time",
        "hierarchy_role": "snapshot",
        "hierarchy_span_start": MOMENT,
        "hierarchy_span_end": MOMENT,
    }
    hints.update(overrides)
    return hints


# -- 映射结果 --------------------------------------------------------------- #


def test_no_hints_yields_empty_structure_and_untouched_metadata() -> None:
    ref, rest = extract_hierarchy_hints({"app": "chat", "device_id": "d1"})

    assert ref.is_empty, "未提供保留键时 hierarchy 保持默认空结构"
    assert rest == {"app": "chat", "device_id": "d1"}


def test_valid_time_leaf_maps_to_leaf_safe_fields() -> None:
    ref, _ = extract_hierarchy_hints(time_leaf())

    assert ref.kind is HierarchyKind.TIME
    assert ref.role is HierarchyRole.SNAPSHOT
    assert ref.span_start == datetime(2026, 8, 18, 9, 32, tzinfo=timezone.utc)
    assert ref.span_end == ref.span_start, "事件点的起止相同"
    assert ref.status is HierarchyStatus.ACTIVE


def test_mapping_never_produces_edges() -> None:
    """接入层只能声明叶身份，父子边一律由构建层建立。"""
    ref, _ = extract_hierarchy_hints(time_leaf(hierarchy_span_end=LATER))

    assert ref.parent_id == ""
    assert ref.child_ids == []
    assert ref.child_scopes == []
    assert ref.parent_scope is None


def test_reserved_keys_are_consumed_and_others_pass_through() -> None:
    ref, rest = extract_hierarchy_hints(time_leaf(app="chat", window_title="IDE"))

    assert not ref.is_empty
    assert rest == {"app": "chat", "window_title": "IDE"}, "保留键落盘前必须被摘掉"


# -- 规则 1：kind/role 同在；区间同在或同缺 --------------------------------- #


@pytest.mark.parametrize(
    "hints",
    [{"hierarchy_kind": "time"}, {"hierarchy_role": "snapshot"}],
    ids=["only_kind", "only_role"],
)
def test_rule1_rejects_half_declared_identity(hints: dict[str, MetadataValueType]) -> None:
    with pytest.raises(ValidationError, match="必须同时提供"):
        extract_hierarchy_hints(hints)


def test_rule1_rejects_span_without_identity() -> None:
    with pytest.raises(ValidationError, match="缺少"):
        extract_hierarchy_hints({"hierarchy_span_start": MOMENT, "hierarchy_span_end": MOMENT})


def test_rule1_rejects_half_declared_span() -> None:
    hints = {"hierarchy_kind": "topic", "hierarchy_role": "node", "hierarchy_span_start": MOMENT}

    with pytest.raises(ValidationError, match="必须同时提供"):
        extract_hierarchy_hints(hints)


# -- 规则 2：枚举精确匹配、时间可解析且有序 --------------------------------- #


@pytest.mark.parametrize(
    ("key", "value"),
    [("hierarchy_kind", "wormhole"), ("hierarchy_role", "not_a_role")],
    ids=["kind", "role"],
)
def test_rule2_rejects_unknown_enum_value(key: str, value: str) -> None:
    with pytest.raises(ValidationError, match="取值非法"):
        extract_hierarchy_hints(time_leaf(**{key: value}))


def test_rule2_rejects_unparsable_time() -> None:
    with pytest.raises(ValidationError, match="不是合法的 ISO 8601 时间"):
        extract_hierarchy_hints(time_leaf(hierarchy_span_start="yesterday"))


def test_rule2_rejects_non_string_time() -> None:
    with pytest.raises(ValidationError, match="必须是 ISO 8601 字符串"):
        extract_hierarchy_hints(time_leaf(hierarchy_span_start=1755509520))


def test_rule2_rejects_reversed_span() -> None:
    with pytest.raises(ValidationError, match="不得晚于"):
        extract_hierarchy_hints(time_leaf(hierarchy_span_start=LATER, hierarchy_span_end=MOMENT))


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-08-18T09:00:00", "2026-08-18T18:00:00+08:00"),
        ("2026-08-18T17:00:00+08:00", "2026-08-18T10:00:00"),
        ("2026-08-18T09:00:00", "2026-08-18T17:00:00+08:00"),
        ("2026-08-18T17:00:00+08:00", "2026-08-18T09:00:00"),
    ],
    ids=["naive-start", "naive-end", "equal-naive-start", "equal-naive-end"],
)
def test_rule2_mixed_timezone_span_uses_utc_without_changing_representation(
    start: str, end: str,
) -> None:
    ref, _ = extract_hierarchy_hints(time_leaf(hierarchy_span_start=start, hierarchy_span_end=end))

    assert ref.span_start.isoformat() == start
    assert ref.span_end.isoformat() == end


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-08-18T09:00:00", "2026-08-18T16:00:00+08:00"),
        ("2026-08-18T18:00:00+08:00", "2026-08-18T09:00:00"),
        ("2026-08-18T09:00:00.000001", "2026-08-18T17:00:00+08:00"),
    ],
    ids=["naive-start", "naive-end", "microsecond-reversal"],
)
def test_rule2_rejects_mixed_timezone_span_reversed_in_utc(start: str, end: str) -> None:
    with pytest.raises(ValidationError, match="不得晚于"):
        extract_hierarchy_hints(time_leaf(hierarchy_span_start=start, hierarchy_span_end=end))


# -- 规则 3：TIME 必须提供区间，其他 kind 可省 ------------------------------ #


def test_rule3_requires_span_for_time_kind() -> None:
    with pytest.raises(ValidationError, match="必须提供区间提示"):
        extract_hierarchy_hints({"hierarchy_kind": "time", "hierarchy_role": "snapshot"})


def test_rule3_allows_missing_span_for_other_kinds() -> None:
    ref, _ = extract_hierarchy_hints({"hierarchy_kind": "topic", "hierarchy_role": "node"})

    assert ref.kind is HierarchyKind.TOPIC
    assert ref.span_start is None and ref.span_end is None


# -- 规则 4：只接受叶角色 --------------------------------------------------- #


@pytest.mark.parametrize(
    "role", ["time_span", "scene", "event", "profile", "root"],
)
def test_rule4_rejects_parent_side_roles(role: str) -> None:
    with pytest.raises(ValidationError, match="必须由构建层创建"):
        extract_hierarchy_hints(time_leaf(hierarchy_role=role))


def test_rule4_rejects_wrong_leaf_role_for_kind() -> None:
    """TIME 的叶角色是 snapshot，其他 kind 是 node，不可互换。"""
    hints = {"hierarchy_kind": "topic", "hierarchy_role": "snapshot"}

    with pytest.raises(ValidationError, match="只接受 topic 的叶角色 node"):
        extract_hierarchy_hints(hints)


# -- 规则 5：试图建边或未定义的保留前缀键一律拒绝 --------------------------- #


@pytest.mark.parametrize(
    "key",
    ["hierarchy_parent_id", "hierarchy_child_ids", "hierarchy_status", "hierarchy_whatever"],
)
def test_rule5_rejects_unlisted_reserved_prefix_keys(key: str) -> None:
    with pytest.raises(ValidationError, match="不接受的 hierarchy 保留键"):
        extract_hierarchy_hints(time_leaf(**{key: "x"}))


def test_rule5_rejects_edge_keys_even_without_other_hints() -> None:
    with pytest.raises(ValidationError, match="父子边只能由构建层建立"):
        extract_hierarchy_hints({"hierarchy_parent_id": "p1"})


# -- 规则 6：任一提示无效即拒绝整条 payload --------------------------------- #


def test_rule6_ingestor_rejects_whole_payload_on_invalid_hint() -> None:
    ingestor = SimpleIngestor(PassthroughNormalizer())
    payload = RawPayload(
        id="p1",
        scope=Scope(org="acme", space="prod", user="zhangsan"),
        data=b"hello",
        system_metadata=time_leaf(hierarchy_kind="wormhole"),
    )

    with pytest.raises(ValidationError):
        ingestor.ingest([payload])


def test_ingestor_maps_valid_hints_end_to_end() -> None:
    ingestor = SimpleIngestor(PassthroughNormalizer())
    payload = RawPayload(
        id="p1",
        scope=Scope(org="acme", space="prod", user="zhangsan", agent="a1", session="s_am"),
        data=b"hello",
        system_metadata=time_leaf(app="chat"),
        user_metadata={"project": "alpha"},
    )

    unit = ingestor.ingest([payload])[0]

    assert unit.hierarchy.kind is HierarchyKind.TIME
    assert unit.hierarchy.role is HierarchyRole.SNAPSHOT
    assert unit.hierarchy.parent_id == "", "接入层不得产出父子边"
    assert "hierarchy_kind" not in unit.system_metadata, "保留键不得落盘为普通 metadata"
    assert unit.system_metadata["app"] == "chat"
    assert unit.user_metadata == {"project": "alpha"}


def test_ingestor_leaves_hierarchy_empty_without_hints() -> None:
    ingestor = SimpleIngestor(PassthroughNormalizer())
    payload = RawPayload(
        id="p1",
        scope=Scope(org="acme", space="prod", user="zhangsan"),
        data=b"hello",
        system_metadata={"app": "chat"},
    )

    unit = ingestor.ingest([payload])[0]

    assert unit.hierarchy.is_empty, "普通写入路径必须保持零行为变化"
    assert unit.system_metadata == {"app": "chat"}


def test_ingestor_does_not_consume_hints_from_user_metadata() -> None:
    hints = time_leaf(hierarchy_parent_id="not-a-real-parent")
    payload = RawPayload(
        id="user-hints", data=b"hello", system_metadata={"app": "chat"}, user_metadata=hints
    )

    unit = SimpleIngestor(PassthroughNormalizer()).ingest([payload])[0]

    assert unit.hierarchy.is_empty
    assert unit.user_metadata == hints
    assert unit.user_metadata is not payload.user_metadata
    assert payload.user_metadata == hints


def test_ingestor_preserves_input_metadata_and_infer_flag() -> None:
    metadata = time_leaf(infer=True, app="chat")
    original = dict(metadata)
    payload = RawPayload(id="source", data=b"hello", system_metadata=metadata)

    unit = SimpleIngestor(PassthroughNormalizer()).ingest([payload])[0]

    assert metadata == original and payload.system_metadata == original
    assert unit.system_metadata == {"infer": True, "app": "chat"}
    assert unit.hierarchy.role is HierarchyRole.SNAPSHOT


def test_invalid_hints_are_rejected_before_normalization(monkeypatch) -> None:
    normalizer = PassthroughNormalizer()
    calls = []

    def normalize(source_payload):
        calls.append(source_payload)
        return "unexpected"

    monkeypatch.setattr(normalizer, "normalize", normalize)
    payload = RawPayload(
        id="invalid", data=b"hello", system_metadata=time_leaf(hierarchy_kind="bad")
    )

    with pytest.raises(ValidationError):
        SimpleIngestor(normalizer).ingest([payload])

    assert calls == [], "无效提示不得进入规约或产生半有效单元"
