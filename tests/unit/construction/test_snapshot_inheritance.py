# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""infer 叶信息传递及四种决策：真实持久化/索引，模型完全离线。"""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial

import pytest

from jiuwen_memory.common.chunker.chunker_impl.recursive_chunker import RecursiveChunker
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    DedupDecision,
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    HierarchyStatus,
    LifecycleState,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.evolver import EvolveMode
from jiuwen_memory.construction.extractor_impl.dynamic_llm_extractor import DynamicLLMExtractor
from jiuwen_memory.construction.extractor_impl.keyword_extractor import KeywordExtractor
from jiuwen_memory.construction.extractor_impl.llm_extractor import ExtractorImpl
from jiuwen_memory.construction.prompt_registry import PromptRegistry
from jiuwen_memory.construction.snapshot_inheritance import (
    inherit_infer_snapshots,
    snapshot_for_decision,
)
from tests.unit.construction.fixtures import (
    MockLLM,
    create_test_unit,
    make_snapshot_test_harness,
    snapshot_test_unit,
)

pytestmark = pytest.mark.unit


@pytest.fixture(params=["orchestrating", "dynamic"])
def harness_factory(request, monkeypatch):
    # Message fixtures are in September; use an explicit evaluation clock for both evolvers.
    monkeypatch.setattr(
        "jiuwen_memory.construction.evolver_impl.orchestrating_evolver._now",
        lambda: datetime(2026, 10, 1, tzinfo=UTC),
    )
    return partial(make_snapshot_test_harness, request.param)


@pytest.fixture
def harness(harness_factory):
    return harness_factory()


@pytest.mark.parametrize("reference", ["source_ref", "provenance"])
def test_transfer_matches_source_and_never_copies_edges(reference):
    source = snapshot_test_unit("source")
    source.system_metadata["infer"] = True
    source.hierarchy.parent_id = "parent"
    source.hierarchy.parent_scope = deepcopy(source.scope)
    source.hierarchy.ordinal = 7
    candidate = create_test_unit("candidate")
    if reference == "source_ref":
        candidate.source_ref = source.id
    else:
        candidate.provenance = [source.id]
    before = deepcopy(source)
    temporal = deepcopy(candidate.temporal)

    inherit_infer_snapshots([source], [candidate])

    assert candidate.hierarchy.kind is HierarchyKind.TIME
    assert candidate.hierarchy.role is HierarchyRole.SNAPSHOT
    assert candidate.hierarchy.span_start == source.hierarchy.span_start
    assert candidate.hierarchy.span_end == source.hierarchy.span_end
    assert candidate.hierarchy.parent_id == "" and candidate.hierarchy.parent_scope is None
    assert candidate.hierarchy.child_ids == [] and candidate.hierarchy.child_scopes == []
    assert candidate.hierarchy.ordinal == 0
    assert candidate.temporal == temporal and source == before


def test_multiple_outputs_get_independent_leaf_objects_from_their_own_source():
    sources = [snapshot_test_unit("first", 1), snapshot_test_unit("second", 20)]
    candidates = [create_test_unit(str(i)) for i in range(3)]
    for source in sources:
        source.system_metadata["infer"] = "TRUE"
    for candidate, source in zip(candidates, [sources[1], sources[0], sources[1]]):
        candidate.source_ref = source.id

    inherit_infer_snapshots(sources, candidates)

    assert [u.hierarchy.span_start.day for u in candidates] == [20, 1, 20]
    candidates[0].hierarchy.parent_id = "new-parent"
    assert candidates[2].hierarchy.parent_id == ""
    assert sources[1].hierarchy.parent_id == ""


def test_source_lookup_uses_scope_for_duplicate_ids():
    first, second = snapshot_test_unit("source", 1), snapshot_test_unit("source", 20)
    second.scope = replace(second.scope, user="bob")
    for source in (first, second):
        source.system_metadata["infer"] = True
    candidate = create_test_unit("candidate", scope=second.scope)
    candidate.source_ref = "source"

    inherit_infer_snapshots([first, second], [candidate])

    assert candidate.hierarchy.span_start == second.hierarchy.span_start


@pytest.mark.parametrize("mode", ["no-infer", "no-hints", "topic"])
def test_unrequested_paths_keep_empty_hierarchy(mode):
    source = snapshot_test_unit("source")
    source.system_metadata["infer"] = mode != "no-infer"
    if mode == "no-hints":
        source.hierarchy = HierarchyRef()
    elif mode == "topic":
        source.hierarchy = HierarchyRef(kind=HierarchyKind.TOPIC, role=HierarchyRole.NODE)
    candidate = create_test_unit("candidate")
    candidate.source_ref = source.id

    inherit_infer_snapshots([source], [candidate])

    assert candidate.hierarchy.is_empty


@pytest.mark.parametrize("bad_reference", ["missing", "multiple", "wrong-scope"])
def test_ambiguous_sources_fail_without_partially_annotating_candidates(bad_reference):
    sources = [snapshot_test_unit("first"), snapshot_test_unit("second", 2)]
    for source in sources:
        source.system_metadata["infer"] = True
    first, second = create_test_unit("good"), create_test_unit("bad")
    first.source_ref = sources[0].id
    if bad_reference == "multiple":
        second.provenance = [source.id for source in sources]
    elif bad_reference == "wrong-scope":
        second.source_ref = sources[0].id
        second.scope = replace(second.scope, user="unknown")

    with pytest.raises(ValidationError, match="来源引用"):
        inherit_infer_snapshots(sources, [first, second])

    assert first.hierarchy.is_empty and second.hierarchy.is_empty


@pytest.mark.parametrize("bad_span", ["missing-attached", "reversed", "dismissed"])
def test_invalid_source_leaf_is_not_propagated(bad_span):
    source = snapshot_test_unit("source", 2)
    source.system_metadata["infer"] = True
    if bad_span == "missing-attached":
        source.hierarchy.span_end = None
        source.hierarchy.parent_id = "parent"
    elif bad_span == "reversed":
        source.hierarchy.span_end = snapshot_test_unit("earlier").hierarchy.span_start
    else:
        source.hierarchy.status = HierarchyStatus.DISMISSED
    candidate = create_test_unit("candidate")
    candidate.source_ref = source.id

    with pytest.raises(ValidationError):
        inherit_infer_snapshots([source], [candidate])

    assert candidate.hierarchy.is_empty


@pytest.mark.parametrize("decision", list(DedupDecision))
def test_decisions_preserve_memory_semantics_and_update_only_snapshot(harness_factory, decision):
    old, candidate = snapshot_test_unit("old"), snapshot_test_unit("new", 20)
    old.provenance, candidate.provenance = ["old-source"], ["new-source"]
    before = deepcopy(old)
    harness = harness_factory(candidate=candidate, existing=old, decision=decision)
    harness.index.build([old])

    result = harness.evolver.evolve([candidate], EvolveMode.EXTRACT)

    stored = loads(harness.world.kv.get(old.scope, memory_key(old.id)))
    count = harness.world.kv.list(old.scope).count
    if decision is DedupDecision.UPDATE:
        assert count == 1 and result.updated_ids == [old.id] and result.created_ids == []
        assert stored.content == "merged old and new", "继续执行原有正文合并"
        assert stored.temporal == before.temporal, "snapshot 覆盖不改变原 temporal 更新语义"
        assert stored.hierarchy == candidate.hierarchy
        assert stored.hierarchy.span_start > before.hierarchy.span_end, "不合并新旧区间"
        assert {"old-source", "new-source"} <= set(stored.provenance)
        assert harness.llm.call_count == 2, "去重判定一次、正文合并一次"
        document = harness.world.fulltext.get(old.scope, [old.id])[0]
        expected_start = int(candidate.hierarchy.span_start.timestamp() * 1000)
        assert document.metadata["tree_span_start"] == expected_start
        assert document.metadata["hierarchy_role"] == "snapshot"
    elif decision is DedupDecision.NOOP:
        assert stored == before and count == 1
        assert result.created_ids == result.updated_ids == result.superseded_ids == []
        assert harness.llm.call_count == 1, "仅去重判定，未调用正文合并"
    else:
        added = loads(harness.world.kv.get(candidate.scope, memory_key(candidate.id)))
        assert count == 2 and result.created_ids == [candidate.id], "不新增额外 snapshot 单元"
        assert added.content == candidate.content and added.hierarchy == candidate.hierarchy
        assert added.hierarchy.parent_id == ""
        if decision is DedupDecision.SUPERSEDE:
            assert stored.lifecycle is LifecycleState.SUPERSEDED
            assert stored.hierarchy == before.hierarchy
            assert added.supersedes == old.id and result.superseded_ids == [old.id]
        else:
            assert stored == before


@pytest.mark.parametrize("old_has_snapshot", [False, True])
def test_update_without_new_hints_preserves_old_hierarchy(harness_factory, old_has_snapshot):
    old = snapshot_test_unit("old") if old_has_snapshot else create_test_unit("old")
    candidate = create_test_unit("new")
    old.temporal.t_message = datetime(2026, 9, 1, tzinfo=UTC)
    candidate.temporal.t_message = datetime(2026, 9, 20, tzinfo=UTC)
    before = deepcopy(old.hierarchy)
    harness = harness_factory(candidate=candidate, existing=old, decision=DedupDecision.UPDATE)
    harness.index.build([old])

    harness.evolver.evolve([candidate], EvolveMode.EXTRACT)

    stored = loads(harness.world.kv.get(old.scope, memory_key(old.id)))
    assert stored.hierarchy == before and stored.content == "merged old and new"


def test_update_can_initialize_an_existing_unstructured_memory(harness_factory):
    old, candidate = create_test_unit("old"), snapshot_test_unit("new", 20)
    old.temporal.t_message = datetime(2026, 9, 1, tzinfo=UTC)
    harness = harness_factory(candidate=candidate, existing=old, decision=DedupDecision.UPDATE)
    harness.index.build([old])

    harness.evolver.evolve([candidate], EvolveMode.EXTRACT)

    stored = loads(harness.world.kv.get(old.scope, memory_key(old.id)))
    assert stored.hierarchy == candidate.hierarchy
    assert stored.id == old.id and harness.world.kv.list(old.scope).count == 1
    assert old.hierarchy is not candidate.hierarchy


@pytest.mark.parametrize("decision", [DedupDecision.UPDATE, DedupDecision.SUPERSEDE])
@pytest.mark.parametrize("edge", ["parent_id", "parent_scope", "child_ids"])
def test_attached_existing_leaf_is_rejected_before_mutation(harness_factory, decision, edge):
    old, candidate = snapshot_test_unit("old"), snapshot_test_unit("new", 20)
    setattr(old.hierarchy, edge, {
        "parent_id": "parent", "parent_scope": old.scope, "child_ids": ["child"],
    }[edge])
    before = deepcopy(old)
    harness = harness_factory(candidate=candidate, existing=old, decision=decision)
    harness.index.build([old])

    with pytest.raises(ValidationError, match="需要结构维护"):
        harness.evolver.evolve([candidate], EvolveMode.EXTRACT)

    assert old == before and harness.llm.call_count == 1, "仅去重判定，未调用正文合并"
    assert loads(harness.world.kv.get(old.scope, memory_key(old.id))) == before
    assert harness.world.kv.list(old.scope).count == 1


def test_noop_on_attached_leaf_remains_a_noop(harness_factory):
    old, candidate = snapshot_test_unit("old"), snapshot_test_unit("new", 20)
    old.hierarchy.parent_id = "parent"
    before = deepcopy(old)
    harness = harness_factory(candidate=candidate, existing=old, decision=DedupDecision.NOOP)
    harness.index.build([old])

    harness.evolver.evolve([candidate], EvolveMode.EXTRACT)

    assert loads(harness.world.kv.get(old.scope, memory_key(old.id))) == before


@pytest.mark.parametrize("decision", [DedupDecision.UPDATE, DedupDecision.SUPERSEDE])
def test_other_tree_roles_cannot_be_overwritten(harness_factory, decision):
    old, candidate = snapshot_test_unit("old"), snapshot_test_unit("new", 20)
    old.hierarchy = HierarchyRef(kind=HierarchyKind.TOPIC, role=HierarchyRole.NODE)
    harness = harness_factory(candidate=candidate, existing=old, decision=decision)
    harness.index.build([old])

    with pytest.raises(ValidationError, match="其他树角色"):
        harness.evolver.evolve([candidate], EvolveMode.EXTRACT)

    assert harness.world.kv.list(old.scope).count == 1
    assert harness.llm.call_count == 1, "仅去重判定，未调用正文合并"


@pytest.mark.parametrize(
    "decision", [DedupDecision.ADD, DedupDecision.UPDATE, DedupDecision.SUPERSEDE],
)
def test_snapshot_without_existing_target_is_valid_and_added(harness_factory, decision):
    candidate = snapshot_test_unit("new", 20)
    # 缺少目标时叶校验允许各写入决策；公开链路在召回为空时直接选择 ADD。
    assert snapshot_for_decision(candidate, decision, None) == candidate.hierarchy
    harness = harness_factory(candidate=candidate, decision=decision)

    result = harness.evolver.evolve([candidate], EvolveMode.EXTRACT)

    stored = loads(harness.world.kv.get(candidate.scope, memory_key(candidate.id)))
    assert result.created_ids == [candidate.id] and stored.hierarchy == candidate.hierarchy
    assert harness.llm.call_count == 0, "无召回目标时直接新增，不调用模型"


@pytest.mark.parametrize("incomplete", [False, True])
def test_infer_evolve_transfers_snapshot_through_real_keyword_extractor(harness, incomplete):
    source = snapshot_test_unit("source")
    if incomplete:
        source.hierarchy.span_end = None
    source.source_ref = "raw-payload-id"
    source.system_metadata["infer"] = True
    before = deepcopy(source)

    result = harness.evolver.evolve([source], EvolveMode.EXTRACT)

    assert len(result.created_units) == 1
    created = result.created_units[0]
    stored = loads(harness.world.kv.get(created.scope, memory_key(created.id)))
    assert stored.hierarchy == source.hierarchy
    assert stored.id != source.id and stored.provenance == [source.id]
    assert harness.world.kv.list(source.scope).count == 1 and source == before


@pytest.mark.parametrize("extractor_kind", ["llm", "dynamic-llm"])
def test_infer_evolve_transfers_snapshot_through_real_llm_extractors(
    harness_factory, extractor_kind,
):
    source = snapshot_test_unit("source")
    source.system_metadata["infer"] = True
    response = json.dumps([{
        "source_id": source.id, "target": "fact", "content": "Derived fact",
        "evidence": source.content, "confidence": 1.0,
    }])
    llm = MockLLM([response])
    extractor = ExtractorImpl(llm=llm)
    if extractor_kind == "dynamic-llm":
        source.system_metadata["_extract_prompt_fact"] = "test-facts"
        extractor = DynamicLLMExtractor(
            llm, KeywordExtractor(RecursiveChunker(chunk_size_chars=200, overlap_chars=0)),
            prompt_registry=PromptRegistry.from_dict({"extract": {"test-facts": "Extract facts"}}),
        )
    # 独立调用 Extractor 仍为空结构；只有 Evolver 的显式 infer 路径赋予叶身份。
    assert extractor.extract([source])[0].hierarchy.is_empty
    harness = harness_factory(extractor=extractor)

    result = harness.evolver.evolve([source], EvolveMode.EXTRACT)

    assert llm.call_count == 2 and len(result.created_units) == 1
    created = result.created_units[0]
    stored = loads(harness.world.kv.get(created.scope, memory_key(created.id)))
    assert stored.hierarchy == source.hierarchy and stored.source_ref == source.id
    assert stored.provenance == [source.id] and stored.content == "Derived fact"
    assert harness.world.kv.list(source.scope).count == 1


@pytest.mark.parametrize("invalid", ["edges", "dismissed", "reversed-span"])
def test_invalid_new_snapshot_is_rejected_before_add(harness_factory, invalid):
    candidate = snapshot_test_unit("new")
    if invalid == "edges":
        candidate.hierarchy.parent_id = "parent"
    elif invalid == "dismissed":
        candidate.hierarchy.status = HierarchyStatus.DISMISSED
    else:
        candidate.hierarchy.span_start = snapshot_test_unit("later", 20).hierarchy.span_start

    harness = harness_factory(candidate=candidate)
    with pytest.raises(ValidationError):
        harness.evolver.evolve([candidate], EvolveMode.EXTRACT)

    assert harness.world.kv.list(candidate.scope).count == 0


@pytest.mark.parametrize("ordinary_kind", ["no-infer", "no-hints"])
@pytest.mark.parametrize("direct_source", [False, True])
def test_mixed_batch_preserves_ordinary_multi_source_candidates(ordinary_kind, direct_source):
    tree = snapshot_test_unit("tree")
    tree.system_metadata["infer"] = True
    ordinary = [snapshot_test_unit("plain-a"), snapshot_test_unit("plain-b")]
    for source in ordinary:
        source.system_metadata["infer"] = ordinary_kind == "no-hints"
        if ordinary_kind == "no-hints":
            source.hierarchy = HierarchyRef()
    leaf, merged = create_test_unit("leaf"), create_test_unit("merged")
    leaf.source_ref = tree.id
    merged.provenance = [source.id for source in ordinary]
    if direct_source:
        merged.source_ref = ordinary[0].id
        merged.provenance.append(tree.id)  # 明确的直接来源优先于 provenance。
    before = deepcopy(merged)

    inherit_infer_snapshots([tree, *ordinary], [leaf, merged])

    assert leaf.hierarchy.span_start == tree.hierarchy.span_start
    assert merged == before, "普通来源不因同批存在 opt-in 来源而被改写或拒绝"


@pytest.mark.parametrize("bad_source", ["tree", "unknown", "other-scope"])
def test_mixed_batch_rejects_ambiguous_or_unresolved_sources_atomically(bad_source):
    tree = snapshot_test_unit("tree")
    tree.system_metadata["infer"] = True
    ordinary, extra = create_test_unit("plain"), create_test_unit("extra")
    good, bad = create_test_unit("good"), create_test_unit("bad")
    good.source_ref = tree.id
    if bad_source == "other-scope":
        extra.scope = replace(extra.scope, session="different")
    reference = {"tree": tree.id, "unknown": "missing", "other-scope": extra.id}[bad_source]
    bad.provenance = [ordinary.id, reference]

    with pytest.raises(ValidationError, match="来源引用"):
        inherit_infer_snapshots([tree, ordinary, extra], [good, bad])

    assert good.hierarchy.is_empty and bad.hierarchy.is_empty


@pytest.mark.parametrize("missing_side", ["old", "new"])
def test_update_accepts_partial_leaf_and_preserves_incoming_time(harness_factory, missing_side):
    old, candidate = snapshot_test_unit("old"), snapshot_test_unit("new", 20)
    incomplete = old if missing_side == "old" else candidate
    incomplete.hierarchy.span_end = None
    harness = harness_factory(candidate=candidate, existing=old, decision=DedupDecision.UPDATE)
    harness.index.build([old])

    result = harness.evolver.evolve([candidate], EvolveMode.EXTRACT)

    stored = loads(harness.world.kv.get(old.scope, memory_key(old.id)))
    assert result.updated_ids == [old.id] and stored.hierarchy == candidate.hierarchy
    assert harness.world.kv.list(old.scope).count == 1
    document = harness.world.fulltext.get(old.scope, [old.id])[0]
    assert ("tree_span_end" in document.metadata) == (candidate.hierarchy.span_end is not None)


def test_older_snapshot_is_expired_without_changing_newer_snapshot(harness_factory):
    old, candidate = snapshot_test_unit("existing", 20), snapshot_test_unit("late", 1)
    before = deepcopy(old)
    harness = harness_factory(candidate=candidate, existing=old, decision=DedupDecision.SUPERSEDE)
    harness.index.build([old])

    result = harness.evolver.evolve([candidate], EvolveMode.EXTRACT)

    stored = loads(harness.world.kv.get(candidate.scope, memory_key(candidate.id)))
    assert loads(harness.world.kv.get(old.scope, memory_key(old.id))) == before
    assert stored.hierarchy == candidate.hierarchy
    assert stored.lifecycle is LifecycleState.SUPERSEDED
    assert result.created_ids == result.superseded_ids == [candidate.id]


def test_snapshot_update_with_unknown_message_time_preserves_both(harness_factory):
    old, candidate = snapshot_test_unit("existing"), snapshot_test_unit("new", 20)
    candidate.temporal.t_message = None
    before = deepcopy(old)
    harness = harness_factory(candidate=candidate, existing=old, decision=DedupDecision.UPDATE)
    harness.index.build([old])

    result = harness.evolver.evolve([candidate], EvolveMode.EXTRACT)

    assert loads(harness.world.kv.get(old.scope, memory_key(old.id))) == before
    stored = loads(harness.world.kv.get(candidate.scope, memory_key(candidate.id)))
    assert stored.hierarchy == candidate.hierarchy
    assert result.created_ids == [candidate.id] and result.updated_ids == []
    assert harness.llm.call_count == 1
