# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Event 摘要与标注只增强父内容，结构先于所有 LLM 调用确定。"""

import json

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole
from jiuwen_memory.construction.hierarchy_composer_impl import DefaultHierarchyComposer
from jiuwen_memory.construction.hierarchy_composer_impl.parent_enrichment import (
    HierarchyModelDependencies,
)
from jiuwen_memory.construction.hierarchy_composer_impl.profile_config import build_profiles
from tests.unit.construction.event_fixtures import EVENT_ROLES, event_profiles
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
)

pytestmark = pytest.mark.unit


def test_event_structure_precedes_summaries_then_annotation_and_root_persistence() -> None:
    leaves = [make_leaf("early"), make_leaf("late", 20)]
    harness = make_harness(leaves)
    profiles = event_profiles(similarity_threshold="0.5", summary_mode="llm")
    stages = profiles[HierarchyKind.TIME].stage_options
    stages["TimeSpanMerger"]["summary_mode"] = "llm"
    stages["SceneSegmenter"]["summary_mode"] = "llm"
    llm = RecordingLLM([
        '{"summary":"第一片段"}', '{"summary":"第二片段"}',
        '{"goal":"设计","actions":"写代码","outcome":"初稿"}',
        '{"goal":"验证","actions":"运行测试","outcome":"通过"}',
        '{"pattern":"开发验证","steps":"设计后测试","outcome":"已完成"}',
    ])
    embedder = RecordingEmbedder([[1, 0], [1, 0]])
    annotator = RecordingAnnotator()
    composer = DefaultHierarchyComposer(harness.builder, profiles,
                                        models=HierarchyModelDependencies(embedder, llm, annotator))
    request = make_request(leaves)
    request.options.parent_roles = list(EVENT_ROLES)
    result = composer.build(request)
    event = harness.read(request.options.tree_home_scope, result.created_parent_ids[0])
    assert result.complete
    assert len(result.created_parent_ids) == 5
    assert embedder.calls == [["- 事实 early：完成开发与验证", "- 事实 late：完成开发与验证"]]
    assert json.loads(llm.calls[-1][1].content) == [
        "目标：设计 行动：写代码 结果：初稿", "目标：验证 行动：运行测试 结果：通过",
    ]
    assert event.segments[1].content == "任务模式：开发验证\n步骤：设计后测试\n结果：已完成"
    assert "2 个场景，2 个片段" in event.segments[0].content
    assert event.layers.l0 == "摘要"
    assert len(event.hierarchy.child_ids) == 2
    assert len(annotator.calls[0]) == 5
    assert all(unit.hierarchy.role is not HierarchyRole.SNAPSHOT for unit in annotator.calls[0])


@pytest.mark.parametrize("response", ["[]", "not json", '{}', '{"pattern":"p","steps":3}',
                                     '{"pattern":"p","steps":"s","outcome":""}',
                                     RuntimeError("LLM unavailable")])
def test_event_summary_failure_keeps_deterministic_content(response, caplog) -> None:
    leaf = make_leaf("fact")
    harness = make_harness([leaf])
    composer = DefaultHierarchyComposer(harness.builder, event_profiles(summary_mode="llm"),
                                        models=HierarchyModelDependencies(llm=RecordingLLM([response])))
    request = make_request([leaf])
    request.options.parent_roles = list(EVENT_ROLES)
    result = composer.build(request)
    parent = harness.read(request.options.tree_home_scope, result.created_parent_ids[0])
    assert result.complete
    assert not result.repair_required
    assert parent.segments[1].content == "- 事实 fact：完成开发与验证"
    assert "1 个场景，1 个片段" in parent.segments[0].content
    assert "summary fallback" in caplog.text


@pytest.mark.parametrize("options", [{"similarity_threshold": "0.5"}, {"summary_mode": "llm"}])
def test_event_model_options_require_explicit_dependencies(options) -> None:
    with pytest.raises(ValidationError):
        DefaultHierarchyComposer(RecordingIndexBuilder(), event_profiles(**options))


def test_event_boundary_and_carried_keys_survive_both_lower_layers() -> None:
    leaves = [make_leaf("a"), make_leaf("b", 1), make_leaf("c", 2)]
    for child, project in zip(leaves, ("A", "B", "A")):
        child.system_metadata.update(project=project, event_type="repair")
    profiles = build_profiles({"time": {
        "parent_roles": ["time_span", "scene", "event"],
        "stage_options": {"EventBuilder": {"boundary_metadata_keys": "project",
                                           "carry_metadata_keys": "event_type"}},
    }})
    harness = make_harness(leaves, profiles=profiles)
    request = make_request(leaves)
    request.options.parent_roles = list(EVENT_ROLES)
    result = harness.composer.build(request)
    assert result.complete
    assert len(result.created_parent_ids) == 9, "下层合并不能吞掉 event 上下文切点"
    for uid in result.created_parent_ids:
        parent = harness.read(request.options.tree_home_scope, uid)
        assert parent.system_metadata["event_type"] == "repair"
        assert parent.system_metadata["project"] in ("A", "B")


@pytest.mark.parametrize("length", [1, 2])
def test_four_level_profile_preserves_shorter_request_prefix(length) -> None:
    recorder = RecordingIndexBuilder()
    composer = DefaultHierarchyComposer(recorder, event_profiles())
    request = make_request([make_leaf("fact")])
    request.options.parent_roles = EVENT_ROLES[:length]
    result = composer.build(request)
    assert result.complete
    assert len(result.created_parent_ids) == length


@pytest.mark.parametrize("roles", [["event"], ["time_span", "event"],
                                   ["time_span", "scene", "event", "event"]])
def test_profile_rejects_event_chain_gaps_or_extra_layers(roles) -> None:
    with pytest.raises(ValidationError):
        build_profiles({"time": {"parent_roles": roles}})


def test_event_options_cannot_be_silently_ignored_in_short_profile() -> None:
    with pytest.raises(ValidationError, match="EventBuilder"):
        build_profiles({"time": {"parent_roles": ["time_span", "scene"],
                                 "stage_options": {"EventBuilder": {}}}})


def test_bad_event_embedding_fails_before_llm_or_persistence() -> None:
    recorder = RecordingIndexBuilder()
    llm = RecordingLLM()
    composer = DefaultHierarchyComposer(
        recorder, event_profiles(similarity_threshold="0.5", summary_mode="llm"),
        models=HierarchyModelDependencies(embedder=RecordingEmbedder([[0, 0]]), llm=llm),
    )
    request = make_request([make_leaf("fact")])
    request.options.parent_roles = list(EVENT_ROLES)
    with pytest.raises(ValidationError):
        composer.build(request)
    assert not recorder.calls
    assert not llm.calls
