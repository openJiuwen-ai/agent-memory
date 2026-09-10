# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Event 的相邻分组、可选判据、严格参数与父内容契约。"""

from copy import deepcopy

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyRole, MemoryTier
from jiuwen_memory.construction.hierarchy_composer_impl.event_pipeline import (
    EventBuilder,
    EventBuilderOptions,
    build_event_parent,
)
from tests.unit.construction.event_fixtures import make_scene
from tests.unit.construction.scene_fixtures import RecordingEmbedder
from tests.unit.construction.time_pipeline_fixtures import TREE_HOME_SCOPE

pytestmark = pytest.mark.unit


def test_default_event_can_span_days_without_mutating_input() -> None:
    scenes = [make_scene("last", 10000), make_scene("first", 0), make_scene("middle", 1500)]
    original = deepcopy(scenes)
    groups = EventBuilder().group(scenes)
    assert [[unit.id for unit in group] for group in groups] == [["first", "middle", "last"]]
    assert scenes == original
    assert EventBuilder().group([]) == []


def test_context_split_does_not_rejoin_nonadjacent_scenes() -> None:
    scenes = [make_scene(str(index), index) for index in range(3)]
    for scene, topic in zip(scenes, ("A", "B", "A")):
        scene.system_metadata["project"] = topic
    groups = EventBuilder(EventBuilderOptions(boundary_metadata_keys=("project",))).group(scenes)
    assert [[unit.id for unit in group] for group in groups] == [["0"], ["1"], ["2"]]


@pytest.mark.parametrize(("before", "after", "count"), [
    (["a", "b"], ["b", "c"], 1),  # equal threshold is accepted
    (["a"], ["b"], 2),
    ([], ["b"], 1),  # missing evidence is not zero overlap
    (["a", "a"], ["a", "b", "b"], 1),  # sets, not raw list lengths
])
def test_entity_overlap_uses_adjacent_set_coefficient(before, after, count) -> None:
    scenes = [make_scene("first"), make_scene("second", 1)]
    scenes[0].entities, scenes[1].entities = before, after
    groups = EventBuilder(EventBuilderOptions(entity_overlap_threshold=0.5)).group(scenes)
    assert len(groups) == count


def test_similarity_uses_body_and_adjacent_pairs() -> None:
    scenes = [make_scene(str(index), index) for index in range(3)]
    embedder = RecordingEmbedder([[1, 0], [0, 1], [1, 0]])
    groups = EventBuilder(EventBuilderOptions(similarity_threshold=0.5), embedder).group(scenes)
    assert len(groups) == 3
    assert len(embedder.calls) == 1
    assert all(scene.segments[0].content not in text
               for scene, text in zip(scenes, embedder.calls[0]))


@pytest.mark.parametrize("vectors", [[], [[0, 0]], [[float("nan"), 1]], [[True, 1]],
                                     [[1, 0], [1]], [[1, 0], [0, float("inf")]]])
def test_bad_event_vectors_fail(vectors) -> None:
    with pytest.raises(ValidationError):
        EventBuilder(EventBuilderOptions(similarity_threshold=0.5),
                     RecordingEmbedder(vectors)).group([make_scene("a"), make_scene("b", 1)])


def test_similarity_requires_explicit_embedder() -> None:
    with pytest.raises(ValidationError, match="embedder"):
        EventBuilder(EventBuilderOptions(similarity_threshold=0.5)).group([make_scene("a")])


@pytest.mark.parametrize("key", ["entity_overlap_threshold", "similarity_threshold"])
@pytest.mark.parametrize("value", [True, -0.1, 1.1, float("nan"), float("inf"), "bad"])
def test_invalid_thresholds_rejected_in_direct_options(key, value) -> None:
    with pytest.raises(ValidationError):
        EventBuilderOptions(**{key: value})


@pytest.mark.parametrize("values", [
    {"settle_seconds": "5"}, {"max_duration_seconds": "86400"}, {"unknown": "value"},
    {"summary_mode": "auto"}, {"summary_max_children": "0"},
    {"summary_max_chars_per_child": True}, {"similarity_threshold": "nan"},
    {"entity_overlap_threshold": "invalid"}, {"entity_overlap_threshold": True},
])
def test_invalid_stage_options_rejected(values) -> None:
    with pytest.raises(ValidationError):
        EventBuilderOptions.from_stage_options(values)


def test_event_parent_preserves_refs_and_only_configured_shared_metadata() -> None:
    scenes = [make_scene("a"), make_scene("b", 3000)]
    for scene in scenes:
        scene.system_metadata = {"project": "P", "event_type": "repair", "infer": "true"}
        scene.user_metadata = {"common": "yes", "different": scene.id}
        scene.entities = ["X", scene.id]
    parent = build_event_parent(
        scenes, tree_home_scope=TREE_HOME_SCOPE,
        options=EventBuilderOptions(boundary_metadata_keys=("project",),
                                    carry_metadata_keys=("event_type", "infer"),
                                    summary_max_children=1, summary_max_chars_per_child=5),
        extra_metadata={"middle": "true", "source": "manual"},
    )
    assert parent.hierarchy.role is HierarchyRole.EVENT
    assert parent.tier is MemoryTier.PROCEDURAL
    assert parent.hierarchy.child_ids == ["a", "b"]
    assert parent.hierarchy.child_scopes == [child_scene.scope for child_scene in scenes]
    assert parent.hierarchy.span_end == scenes[-1].hierarchy.span_end
    assert parent.system_metadata == {"project": "P", "event_type": "repair", "source": "manual"}
    assert parent.user_metadata == {"common": "yes"}
    assert parent.entities == ["X", "a", "b"]
    assert not parent.provenance
    assert "2 个场景" in parent.segments[0].content
    assert "另有 1 个场景" in parent.segments[1].content


def test_event_rejects_empty_or_wrong_role_children() -> None:
    with pytest.raises(ValidationError):
        build_event_parent([], tree_home_scope=TREE_HOME_SCOPE, options=EventBuilderOptions())
    scene = make_scene("bad")
    scene.hierarchy.role = HierarchyRole.TIME_SPAN
    with pytest.raises(ValidationError, match="scene"):
        EventBuilder().group([scene])
