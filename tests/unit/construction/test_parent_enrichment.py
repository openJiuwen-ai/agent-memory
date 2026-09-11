# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""父语义摘要及 L0/L1 的配置、降级与零事实改写边界。"""

import json
from copy import deepcopy

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposerProducer
from jiuwen_memory.construction.hierarchy_composer_impl import DefaultHierarchyComposer
from jiuwen_memory.construction.hierarchy_composer_impl.parent_enrichment import (
    HierarchyModelDependencies,
)
from jiuwen_memory.construction.hierarchy_composer_impl.profile_config import build_profiles
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.construction.layer_annotator_impl.keyword_layer_annotator import (
    KeywordLayerAnnotator,
)
from tests.unit.construction.hierarchy_fixtures import (
    RecordingIndexBuilder,
    make_harness,
    make_leaf,
    make_request,
)
from tests.unit.construction.scene_fixtures import (
    RecordingAnnotator,
    RecordingEmbedder,
    RecordingLLM,
    summary_profiles,
)

pytestmark = pytest.mark.unit


def test_structure_before_llm_summary_then_parent_only_annotation() -> None:
    leaves = [make_leaf("a"), make_leaf("b", 20)]
    harness = make_harness(leaves)
    llm = RecordingLLM([
        '{"summary":"启动锁设计"}', '{"summary":"完成锁验证"}',
        '{"goal":"实现锁","actions":"设计和验证","outcome":"通过"}',
    ])
    embedder = RecordingEmbedder([[1, 0], [1, 0]])
    annotator = RecordingAnnotator()
    composer = DefaultHierarchyComposer(harness.builder, summary_profiles(),
                                        models=HierarchyModelDependencies(embedder, llm, annotator))
    request = make_request(leaves)
    request.options.parent_roles.append(HierarchyRole.SCENE)
    original = deepcopy(request)

    result = composer.build(request)

    assert result.complete
    parents = [harness.read(request.options.tree_home_scope, uid)
               for uid in result.created_parent_ids]
    assert len(parents) == 3
    assert embedder.calls == [["- 事实 a：完成开发与验证", "- 事实 b：完成开发与验证"]]
    assert len(llm.calls) == 3
    assert json.loads(llm.calls[2][1].content) == ["启动锁设计", "完成锁验证"]
    assert parents[0].segments[1].content == "目标：实现锁\n行动：设计和验证\n结果：通过"
    assert "2 个片段，2 条记录" in parents[0].segments[0].content
    assert parents[0].hierarchy.child_ids
    assert all(parent.layers.l0 == "摘要" for parent in parents)
    assert all(unit.hierarchy.role is not HierarchyRole.SNAPSHOT for unit in annotator.calls[0])
    assert request == original
    for leaf in leaves:
        persisted = harness.read(leaf.scope, leaf.id)
        persisted.hierarchy = deepcopy(leaf.hierarchy)
        assert persisted == leaf


@pytest.mark.parametrize("response", [RuntimeError("unavailable"), "not json", "[]",
                                     '{"summary":""}', '{"summary":3}', '{}'])
def test_llm_failure_keeps_structural_excerpt_and_does_not_mark_repair(response, caplog) -> None:
    leaf = make_leaf("fact")
    harness = make_harness([leaf])
    profiles = build_profiles({"time": {"parent_roles": ["time_span"], "stage_options": {
        "TimeSpanMerger": {"summary_mode": "llm"},
    }}})
    composer = DefaultHierarchyComposer(harness.builder, profiles,
                                        models=HierarchyModelDependencies(llm=RecordingLLM([response])))
    request = make_request([leaf])
    result = composer.build(request)
    parent = harness.read(request.options.tree_home_scope, result.created_parent_ids[0])
    assert result.complete
    assert not result.repair_required
    assert parent.segments[1].content == "- 事实 fact：完成开发与验证"
    assert "summary fallback" in caplog.text


@pytest.mark.parametrize("failure", [False, True])
def test_layer_failure_and_unconfigured_llm_preserve_structural_content(failure) -> None:
    leaf = make_leaf("fact")
    harness = make_harness([leaf])
    llm = RecordingLLM()
    annotator = RecordingAnnotator(failure)
    composer = DefaultHierarchyComposer(harness.builder,
                                        models=HierarchyModelDependencies(llm=llm,
                                                                          layer_annotator=annotator))
    request = make_request([leaf])
    result = composer.build(request)
    parent = harness.read(request.options.tree_home_scope, result.created_parent_ids[0])
    assert result.complete
    assert not llm.calls, "未启用 summary_mode=llm，即使注入模型也不得调用"
    assert parent.segments[1].content == "- 事实 fact：完成开发与验证"
    assert bool(parent.layers.l0) is not failure
    assert parent.hierarchy.child_ids == [leaf.id]


@pytest.mark.parametrize("threshold, annotated", [(0, True), (10000, False)])
def test_existing_annotator_threshold_is_honored(threshold, annotated) -> None:
    leaf = make_leaf("fact")
    harness = make_harness([leaf])
    composer = DefaultHierarchyComposer(harness.builder, models=HierarchyModelDependencies(
        layer_annotator=KeywordLayerAnnotator(layers_threshold=threshold),
    ))
    request = make_request([leaf])
    result = composer.build(request)
    parent = harness.read(request.options.tree_home_scope, result.created_parent_ids[0])
    assert bool(parent.layers.l0) is annotated
    assert bool(parent.layers.l1) is annotated


@pytest.mark.parametrize("models", [HierarchyModelDependencies(),
                                   HierarchyModelDependencies(llm=RecordingLLM()),
                                   HierarchyModelDependencies(embedder=RecordingEmbedder())])
def test_required_model_dependency_is_checked_at_assembly(models) -> None:
    with pytest.raises(ValidationError):
        DefaultHierarchyComposer(RecordingIndexBuilder(), summary_profiles(), models=models)


def test_factory_explicit_dependencies_are_available_without_changing_request_protocol() -> None:
    recorder = RecordingIndexBuilder()
    IndexBuilderProducer.put("scene-index", recorder)
    try:
        composer = HierarchyComposerProducer.build("default", {
            "index_builder": "scene-index",
            "layer_annotator": {"target": "keyword", "params": {"layer_annotator_threshold": 0}},
            "hierarchy_profiles": {"time": {"parent_roles": ["time_span", "scene"]}},
        }, AssemblyContext())
        request = make_request([make_leaf("fact")])
        request.options.parent_roles.append(HierarchyRole.SCENE)
        result = composer.build(request)
        assert result.complete
        assert all(unit.layers.l0 for unit in recorder.calls[0].units)
    finally:
        IndexBuilderProducer.reset_instances()


def test_profile_can_use_shorter_prefix_but_cannot_extend_beyond_configured_chain() -> None:
    request = make_request([make_leaf("fact")])
    broad = build_profiles({"time": {"parent_roles": ["time_span", "scene"]}})
    assert DefaultHierarchyComposer(RecordingIndexBuilder(), broad).build(request).complete
    narrow = build_profiles({"time": {"parent_roles": ["time_span"]}})
    request.options.parent_roles.append(HierarchyRole.SCENE)
    recorder = RecordingIndexBuilder()
    with pytest.raises(ValidationError, match="profile"):
        DefaultHierarchyComposer(recorder, narrow).build(request)
    assert not recorder.calls
    assert broad[HierarchyKind.TIME].parent_roles == (HierarchyRole.TIME_SPAN, HierarchyRole.SCENE)


def test_summary_material_follows_child_order_and_enforces_input_bounds() -> None:
    leaves = [make_leaf("later", 20), make_leaf("earlier", 0), make_leaf("last", 40)]
    harness = make_harness(leaves)
    llm = RecordingLLM(['{"summary":"done"}'])
    profiles = build_profiles({"time": {"parent_roles": ["time_span"], "stage_options": {
        "TimeSpanMerger": {"summary_mode": "llm", "summary_max_leaves": 2,
                           "summary_max_chars_per_leaf": 9},
    }}})
    composer = DefaultHierarchyComposer(harness.builder, profiles,
                                        models=HierarchyModelDependencies(llm=llm))
    result = composer.build(make_request(leaves))
    assert result.complete
    assert json.loads(llm.calls[0][1].content) == [leaves[1].content[:9], leaves[0].content[:9]]


@pytest.mark.parametrize("response", ['{"goal":"g"}', '{"goal":1,"actions":"a","outcome":"o"}',
                                     '{"goal":"g","actions":"","outcome":"o"}', "[]"])
def test_invalid_scene_summary_retains_deterministic_body_and_code_counts(response) -> None:
    leaf = make_leaf("fact")
    harness = make_harness([leaf])
    profiles = build_profiles({"time": {"parent_roles": ["time_span", "scene"], "stage_options": {
        "SceneSegmenter": {"summary_mode": "llm"},
    }}})
    composer = DefaultHierarchyComposer(harness.builder, profiles,
                                        models=HierarchyModelDependencies(llm=RecordingLLM([response])))
    request = make_request([leaf])
    request.options.parent_roles.append(HierarchyRole.SCENE)
    result = composer.build(request)
    scene = harness.read(request.options.tree_home_scope, result.created_parent_ids[0])
    assert result.complete
    assert scene.segments[1].content == "- 事实 fact：完成开发与验证"
    assert "1 个片段，1 条记录" in scene.segments[0].content


def test_invalid_embedder_result_is_fatal_before_any_persistence() -> None:
    recorder = RecordingIndexBuilder()
    dependencies = HierarchyModelDependencies(
        embedder=RecordingEmbedder([[0, 0]]), llm=RecordingLLM(),
    )
    composer = DefaultHierarchyComposer(recorder, summary_profiles(), models=dependencies)
    request = make_request([make_leaf("fact")])
    request.options.parent_roles.append(HierarchyRole.SCENE)
    with pytest.raises(ValidationError):
        composer.build(request)
    assert not recorder.calls


@pytest.mark.parametrize("stage_key, metadata", [
    ("boundary_metadata_keys", [{"context": "one"}, {"context": "two"}]),
    ("end_signal_metadata_keys", [{"context": "done"}, {}]),
])
def test_scene_boundaries_are_preserved_through_time_span_merging(stage_key, metadata) -> None:
    leaves = [make_leaf("a"), make_leaf("b", 1)]
    for child, values in zip(leaves, metadata):
        child.system_metadata.update(values)
    harness = make_harness(leaves)
    profiles = build_profiles({"time": {"parent_roles": ["time_span", "scene"], "stage_options": {
        "SceneSegmenter": {stage_key: "context"},
    }}})
    composer = DefaultHierarchyComposer(harness.builder, profiles)
    request = make_request(leaves)
    request.options.parent_roles.append(HierarchyRole.SCENE)
    result = composer.build(request)
    assert result.complete
    assert len(result.created_parent_ids) == 4, "底层合并不能先吞掉上层要求的上下文或结束切点"


@pytest.mark.parametrize("fail_at, created_count, updated_count", [(1, 0, 0), (2, 1, 0), (3, 2, 0)])
def test_three_level_body_failure_never_switches_leaves_before_all_parents(
    fail_at, created_count, updated_count,
) -> None:
    """按根→中间父→叶切边顺序故障注入，旧叶保持原样。"""
    leaf = make_leaf("fact")
    harness = make_harness([leaf])
    harness.builder.fail_at = fail_at
    request = make_request([leaf])
    request.options.parent_roles.append(HierarchyRole.SCENE)
    result = harness.composer.build(request)
    assert not result.complete
    assert len(result.created_parent_ids) == created_count
    assert len(result.updated_child_ids) == updated_count
    assert harness.read(leaf.scope, leaf.id) == leaf
