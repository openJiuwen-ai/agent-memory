# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scene 的确定性分组、显式可选判据与字段隔离。"""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import timedelta, timezone

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyRole
from jiuwen_memory.construction.hierarchy_composer_impl.scene_pipeline import (
    SceneSegmenter,
    SceneSegmenterOptions,
    build_scene_parent,
)
from tests.unit.construction.scene_fixtures import RecordingEmbedder, make_span
from tests.unit.construction.time_pipeline_fixtures import TREE_HOME_SCOPE, group_ids

pytestmark = pytest.mark.unit


def test_defaults_stable_order_and_no_session_boundary() -> None:
    options = SceneSegmenterOptions()
    assert options.max_duration_seconds == 86400
    assert options.similarity_threshold is None
    assert not options.end_signal_metadata_keys
    with pytest.raises(FrozenInstanceError):
        options.max_duration_seconds = 1
    spans = [make_span("b", 20), make_span("a", 0)]
    spans[0].scope.session = "second"
    spans[1].scope.session = "first"
    before = deepcopy(spans)
    assert group_ids(SceneSegmenter().segment(spans)) == [["a", "b"]]
    assert spans == before
    assert SceneSegmenter().segment([]) == []


@pytest.mark.parametrize("extra_microseconds, count", [(-1, 1), (0, 1), (1, 2)])
def test_duration_boundary_uses_utc_and_microsecond_precision(extra_microseconds, count) -> None:
    first, last = make_span("first"), make_span("last", 1440)
    first.hierarchy.span_start = first.hierarchy.span_start.replace(tzinfo=None)
    last.hierarchy.span_end += timedelta(microseconds=extra_microseconds)
    last.hierarchy.span_start = last.hierarchy.span_end.astimezone(timezone(timedelta(hours=8)))
    assert len(SceneSegmenter().segment([first, last])) == count


def test_duration_is_whole_group_and_does_not_split_atomic_long_span() -> None:
    segmenter = SceneSegmenter(SceneSegmenterOptions(max_duration_seconds=3600))
    spans = [make_span("a"), make_span("b", 40), make_span("c", 80)]
    assert group_ids(segmenter.segment(spans)) == [["a", "b"], ["c"]]
    assert group_ids(segmenter.segment([make_span("long", 0, 120), make_span("nested", 10)])) == [
        ["long"], ["nested"],
    ]


def test_system_context_and_previous_end_signal_are_opt_in() -> None:
    spans = [make_span("a"), make_span("b", 1), make_span("c", 2)]
    spans[0].system_metadata = {"device": "desktop"}
    spans[1].system_metadata = {"device": "phone", "done": "yes"}
    spans[2].system_metadata = {"device": "phone"}
    assert len(SceneSegmenter().segment(spans)) == 1
    segmenter = SceneSegmenter(SceneSegmenterOptions(
        boundary_metadata_keys=("device",), end_signal_metadata_keys=("done",),
    ))
    assert group_ids(segmenter.segment(spans)) == [["a"], ["b"], ["c"]]
    for span in spans:
        span.user_metadata = span.system_metadata
        span.system_metadata = {}
    assert len(segmenter.segment(spans)) == 1


def test_similarity_compares_adjacent_bodies_without_time_headers() -> None:
    embedder = RecordingEmbedder([[1, 0], [1, 0], [0, 1]])
    segmenter = SceneSegmenter(SceneSegmenterOptions(similarity_threshold=0.5), embedder)
    assert group_ids(segmenter.segment([make_span("a"), make_span("b", 1), make_span("c", 2)])) == [
        ["a", "b"], ["c"],
    ]
    assert embedder.calls == [["- a-leaf 的内容", "- b-leaf 的内容", "- c-leaf 的内容"]]
    disabled = RecordingEmbedder()
    SceneSegmenter(embedder=disabled).segment([make_span("a")])
    assert not disabled.calls


def test_similarity_equal_threshold_does_not_split() -> None:
    segmenter = SceneSegmenter(SceneSegmenterOptions(similarity_threshold=0),
                               RecordingEmbedder([[1, 0], [0, 1]]))
    assert len(segmenter.segment([make_span("a"), make_span("b")])) == 1


@pytest.mark.parametrize("vectors", [[], [[1, 0]], [[1, 0], [1]], [[0, 0], [1, 0]],
                                   [[float("nan"), 0], [1, 0]], [[True, 0], [1, 0]]])
def test_invalid_vectors_fail_instead_of_silently_changing_groups(vectors) -> None:
    segmenter = SceneSegmenter(SceneSegmenterOptions(similarity_threshold=0.5),
                               RecordingEmbedder(vectors))
    with pytest.raises(ValidationError):
        segmenter.segment([make_span("a"), make_span("b")])


@pytest.mark.parametrize("values", [
    {"similarity_threshold": "nan"}, {"similarity_threshold": "inf"},
    {"similarity_threshold": "-0.1"}, {"similarity_threshold": "1.1"},
    {"similarity_threshold": True}, {"similarity_threshold": "x"},
    {"max_duration_seconds": "0"}, {"summary_max_children": "-1"},
    {"summary_max_chars_per_child": "0"}, {"summary_mode": "unknown"},
    {"settle_seconds": "60"}, {"entity_overlap_threshold": "0.5"},
])
def test_invalid_scene_configuration_is_rejected(values) -> None:
    with pytest.raises(ValidationError):
        SceneSegmenterOptions.from_stage_options(values)


@pytest.mark.parametrize("values", [
    {"similarity_threshold": True}, {"similarity_threshold": "0.5"},
    {"max_duration_seconds": False}, {"boundary_metadata_keys": ["key"]},
    {"end_signal_metadata_keys": ("",)}, {"carry_metadata_keys": (1,)},
])
def test_invalid_direct_scene_options_are_rejected(values) -> None:
    with pytest.raises(ValidationError):
        SceneSegmenterOptions(**values)


def test_scene_parent_inherits_only_shared_metadata_and_code_derived_counts() -> None:
    children = [make_span("a"), make_span("b", 10)]
    for span in children:
        span.system_metadata = {"shared": "yes", "infer": "false"}
        span.user_metadata = {"same": [1, 2], "different": span.id}
        span.entities = ["Memory", span.id]
    original = deepcopy(children)
    parent = build_scene_parent(children, tree_home_scope=TREE_HOME_SCOPE,
                                options=SceneSegmenterOptions(
                                    carry_metadata_keys=("shared", "infer"), summary_max_children=1,
                                ), extra_metadata={"source": "manual", "middle": "true"})
    assert parent.hierarchy.role is HierarchyRole.SCENE
    assert parent.hierarchy.child_ids == ["a", "b"]
    assert parent.hierarchy.child_scopes == [TREE_HOME_SCOPE, TREE_HOME_SCOPE]
    assert parent.system_metadata == {"shared": "yes", "source": "manual"}
    assert parent.user_metadata == {"same": [1, 2]}
    assert parent.entities == ["Memory", "a", "b"]
    assert "2 个片段，2 条记录" in parent.segments[0].content
    assert "另有 1 个片段" in parent.segments[1].content
    assert not parent.provenance
    assert not parent.layers.l0
    assert children == original
