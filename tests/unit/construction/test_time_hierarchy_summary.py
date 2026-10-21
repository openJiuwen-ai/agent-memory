# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""纯流水线验证逐层摘要、完整事实及上层分组；不依赖持久化或查询。"""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, Segment, validate_tree
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeOptions
from jiuwen_memory.construction.hierarchy_composer_impl.event_pipeline import EventBuilderOptions
from jiuwen_memory.construction.hierarchy_composer_impl.parent_enrichment import (
    HierarchyModelDependencies,
)
from jiuwen_memory.construction.hierarchy_composer_impl.scene_pipeline import SceneSegmenterOptions
from jiuwen_memory.construction.hierarchy_composer_impl.time_hierarchy_pipeline import (
    TimeHierarchyPipeline,
)
from jiuwen_memory.construction.hierarchy_composer_impl.time_pipeline import TimeSpanMergerOptions
from tests.unit.construction.scene_fixtures import (
    RecordingAnnotator,
    RecordingEmbedder,
    RecordingLLM,
)
from tests.unit.construction.time_pipeline_fixtures import TREE_HOME_SCOPE, make_leaf

pytestmark = pytest.mark.unit
ROLES = [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.EVENT]


def options(depth=3):
    return HierarchyComposeOptions(HierarchyKind.TIME, HierarchyRole.SNAPSHOT,
                                   ROLES[:depth], TREE_HOME_SCOPE)


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("fallback", [False, True])
def test_complete_facts_survive_abbreviated_summaries_and_excerpt_limits(depth, fallback):
    leaves = [make_leaf(str(i), i) for i in range(25)]
    leaves[-1].segments = [Segment(content="background " * 50 + "Maya visited on July 5")]
    original = deepcopy(leaves)
    llm = RecordingLLM([
        RuntimeError("unavailable") if fallback else '{"summary":"activity"}',
        '{"goal":"visit","actions":"travel","outcome":"done"}',
        '{"pattern":"travel","steps":"visit","outcome":"done"}',
    ])
    pipeline = TimeHierarchyPipeline(
        TimeSpanMergerOptions(summary_mode="llm", summary_max_leaves=1,
                              summary_max_chars_per_leaf=5),
        SceneSegmenterOptions(summary_mode="llm", summary_max_children=1,
                              summary_max_chars_per_child=5),
        HierarchyModelDependencies(llm=llm),
        EventBuilderOptions(summary_mode="llm", summary_max_children=1,
                            summary_max_chars_per_child=5),
    )
    parents, children = pipeline.build(leaves, options(depth))
    assert len(parents) == depth
    assert len(llm.calls) == depth
    assert json.loads(llm.calls[0][1].content) == [leaf.content for leaf in leaves]
    for parent in parents:
        assert [fact.text for fact in parent.tree_memory.facts] == [leaf.content for leaf in leaves]
    for call in llm.calls[1:]:
        assert json.loads(call[1].content)[0]["facts"] == [leaf.content for leaf in leaves]
    validate_tree([*parents, *children])
    assert leaves == original
    for before, child in zip(original, children):
        child.hierarchy = deepcopy(before.hierarchy)
        assert child == before


@pytest.mark.parametrize("role", [HierarchyRole.SCENE, HierarchyRole.EVENT])
def test_upper_grouping_uses_completed_child_summaries(role):
    leaves = [make_leaf("a", 0), make_leaf("b", 20)]
    responses = ['{"summary":"first summary"}', '{"summary":"second summary"}']
    scene = SceneSegmenterOptions(max_duration_seconds=1)
    event = EventBuilderOptions()
    if role is HierarchyRole.SCENE:
        scene = replace(scene, max_duration_seconds=86400, similarity_threshold=0.65)
        expected = ["first summary", "second summary"]
    else:
        scene = replace(scene, summary_mode="llm")
        responses += ['{"goal":"G1","actions":"A1","outcome":"O1"}',
                      '{"goal":"G2","actions":"A2","outcome":"O2"}']
        event = replace(event, similarity_threshold=0.65)
        expected = ["目标：G1\n行动：A1\n结果：O1", "目标：G2\n行动：A2\n结果：O2"]
    embedder = RecordingEmbedder([[1, 0], [1, 0]])
    annotator = RecordingAnnotator()
    pipeline = TimeHierarchyPipeline(
        TimeSpanMergerOptions(gap_seconds=1, summary_mode="llm"), scene,
        HierarchyModelDependencies(embedder, RecordingLLM(responses), annotator), event,
    )
    parents, children = pipeline.build(leaves, options())
    assert embedder.calls == [expected]
    assert all(parent.layers.l0 == "摘要" for parent in parents)
    assert all(parent.hierarchy.child_ids for parent in parents)
    assert len(annotator.calls) == 1
    assert len(annotator.calls[0]) == len(parents)
    validate_tree([*parents, *children])


@pytest.mark.parametrize("response", ["not json", "[]", '{}', '{"summary":""}',
                                     '{"summary":3}', RuntimeError("timeout")])
def test_invalid_summary_preserves_structural_evidence(response):
    pipeline = TimeHierarchyPipeline(
        merger=TimeSpanMergerOptions(summary_mode="llm"),
        models=HierarchyModelDependencies(llm=RecordingLLM([response])),
    )
    leaf = make_leaf("a", 0)
    parents, children = pipeline.build([leaf], options(1))
    assert leaf.content in parents[0].segments[1].content
    assert parents[0].tree_memory.facts[0].text == leaf.content
    validate_tree([*parents, *children])


@pytest.mark.parametrize("pipeline", [
    TimeHierarchyPipeline(merger=TimeSpanMergerOptions(summary_mode="llm")),
    TimeHierarchyPipeline(segmenter=SceneSegmenterOptions(similarity_threshold=0.5)),
    TimeHierarchyPipeline(event_builder=EventBuilderOptions(similarity_threshold=0.5)),
])
def test_enabled_model_features_require_dependencies(pipeline):
    with pytest.raises(ValidationError):
        pipeline.build([make_leaf("a", 0)], options())


def test_naive_and_aware_inputs_keep_identical_grouping_and_original_leaf_times():
    leaves = [make_leaf("a", 0), make_leaf("b", 120), make_leaf("c", 121)]
    naive = deepcopy(leaves)
    aware = deepcopy(leaves)
    for unit in naive:
        unit.hierarchy.span_start = unit.hierarchy.span_start.replace(tzinfo=None)
        unit.hierarchy.span_end = unit.hierarchy.span_end.replace(tzinfo=None)
    zone = timezone(timedelta(hours=8))
    for unit in aware:
        unit.hierarchy.span_start = unit.hierarchy.span_start.astimezone(zone)
        unit.hierarchy.span_end = unit.hierarchy.span_end.astimezone(zone)
    outcomes = []
    for inputs in (leaves, naive, aware):
        original = deepcopy(inputs)
        parents, children = TimeHierarchyPipeline().build(inputs, options(1))
        outcomes.append([(p.hierarchy.child_ids, p.hierarchy.span_start, p.hierarchy.span_end)
                         for p in parents])
        assert inputs == original
        for child, before in zip(children, original):
            assert child.hierarchy.span_start == before.hierarchy.span_start
            assert child.hierarchy.span_end == before.hierarchy.span_end
    assert outcomes[0] == outcomes[1] == outcomes[2]
