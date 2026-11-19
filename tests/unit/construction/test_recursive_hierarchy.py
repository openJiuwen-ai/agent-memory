"""Recursive public composer invariants, using actual forward/index persistence."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.graphmem_structure import node_height
from jiuwen_memory.common.type_def import HierarchyRole, Scope, Segment, validate_tree
from jiuwen_memory.construction.hierarchy_composer_impl.recursive_composer import (
    RecursiveHierarchyComposer,
)
from jiuwen_memory.construction.hierarchy_composer_impl.recursive_summary import RecursivePolicy
from tests.unit.construction.hierarchy_fixtures import (
    ORIGIN,
    RecordingIndexBuilder,
    make_harness,
    make_leaf,
    make_request,
)

pytestmark = pytest.mark.unit


class SummaryModel:
    def __init__(self):
        self.calls = []
        self.invalid = False

    @staticmethod
    def health():
        return None

    @staticmethod
    def plugin_type():
        from jiuwen_memory.common.base import PluginType

        return PluginType.LLM

    def chat(self, messages, **kwargs):
        records = json.loads(messages[-1].content)
        self.calls.append(records)
        return json.dumps({
            "details": {str(r["id"]): "x" * 2000 if self.invalid
                        else f"Evidence {r['id']} retained." for r in records},
            "entities": [], "l0": "Evidence", "l1": "Recorded evidence overview",
        })


def recursive_harness(leaves, **policy):
    harness = make_harness(leaves)
    model = SummaryModel()
    harness.composer = RecursiveHierarchyComposer(
        harness.builder, model, len, tokenizer_id="test-character-counter",
        policy=RecursivePolicy(**policy),
    )
    return harness, model


def read_tree(harness, request, result):
    parents = [harness.read(request.options.tree_home_scope, uid)
               for uid in result.created_parent_ids]
    leaves = [harness.read(u.scope, u.id) for u in request.leaves]
    return leaves, parents


def test_more_than_four_levels_and_no_raw_copies():
    original = [make_leaf(f"u{i}", i) for i in range(65)]
    harness, model = recursive_harness(original, max_children=2)
    request = make_request(original)
    result = harness.composer.build(request)
    leaves, parents = read_tree(harness, request, result)
    assert result.complete
    assert max(node_height(p) for p in parents) == 7
    assert all(len(p.hierarchy.child_ids) <= 2 and p.tree_memory is None for p in parents)
    validate_tree(leaves + parents)
    assert sum(not p.hierarchy.parent_id for p in parents) == 1
    assert sorted(uid for p in parents for uid in p.hierarchy.child_ids if uid.startswith("u")) \
        == sorted(u.id for u in original)
    assert [u.content for u in leaves] == [u.content for u in original]
    assert all(not u.hierarchy.parent_id for u in original), "request objects were mutated"
    assert len(model.calls) == len(parents)
    assert max(harness.composer.summary.call_input_tokens) <= 6000


def test_default_k_ten_grows_a_fourth_parent_level():
    leaves = [make_leaf(f"u{i}", i) for i in range(1001)]
    builder = RecordingIndexBuilder()
    composer = RecursiveHierarchyComposer(builder, SummaryModel(), len,
                                          tokenizer_id="test-counter")
    result = composer.build(make_request(leaves))
    parents = {u.id: u for call in builder.calls for u in call.units
               if u.hierarchy.role is HierarchyRole.TIME_SPAN}
    assert result.complete
    assert max(node_height(p) for p in parents.values()) == 4
    assert max(len(p.hierarchy.child_ids) for p in parents.values()) <= 10
    assert all(p.tree_memory is None for p in parents.values())


def test_token_budget_reduces_actual_fanout_below_configured_k():
    leaves = [make_leaf(f"u{i}", i) for i in range(20)]
    for leaf in leaves:
        leaf.segments = [Segment(content="material " * 90)]
    harness, _ = recursive_harness(leaves, max_children=16)
    request = make_request(leaves)
    result = harness.composer.build(request)
    _, parents = read_tree(harness, request, result)
    assert result.complete
    assert max(len(p.hierarchy.child_ids) for p in parents if node_height(p) == 1) < 16
    assert max(harness.composer.summary.call_input_tokens) <= 6000


def test_append_reuses_sealed_subtrees_and_repeat_is_noop():
    original = [make_leaf(f"u{i}", i) for i in range(8)]
    harness, model = recursive_harness(original, max_children=2)
    request = make_request(original)
    built = harness.composer.build(request)
    leaves, parents = read_tree(harness, request, built)
    calls_before = len(model.calls)
    extra = make_leaf("new", 9)
    harness.builder.build([extra])
    appended = make_request(leaves + [extra], parents=parents,
                            span=(ORIGIN, ORIGIN + timedelta(hours=1)))
    result = harness.composer.replace_in_span(appended)
    assert result.complete and len(result.created_parent_ids) == 1
    assert not result.replaced_parent_ids
    assert len(model.calls) == calls_before + 1, "unchanged summaries were regenerated"
    current_parents = [harness.read(request.options.tree_home_scope, uid)
                       for uid in [*built.created_parent_ids, *result.created_parent_ids]]
    current_leaves = [harness.read(u.scope, u.id) for u in leaves + [extra]]
    validate_tree(current_leaves + current_parents)
    before_writes, before_models = len(harness.builder.calls), len(model.calls)
    repeat = harness.composer.replace_in_span(make_request(
        current_leaves, parents=current_parents,
        span=(ORIGIN, ORIGIN + timedelta(hours=1))))
    assert repeat.complete and not repeat.created_parent_ids
    assert len(harness.builder.calls) == before_writes and len(model.calls) == before_models


def test_sessions_never_merge_even_with_same_source_id():
    leaves = [make_leaf("same", scope=Scope(org="o", user="a", session=session))
              for session in ("s1", "s2")]
    harness, _ = recursive_harness(leaves)
    request = make_request(leaves)
    result = harness.composer.build(request)
    _, parents = read_tree(harness, request, result)
    assert len(parents) == 2 and all(not p.hierarchy.parent_id for p in parents)
    assert len({p.system_metadata["graphmem_session_scope"] for p in parents}) == 2


def test_ingest_fragments_keep_source_order_and_reordered_collection_reuses_tree():
    # Equal timestamps and random IDs cannot provide the order of one payload.
    original = [make_leaf(uid) for uid in ("c", "a", "d", "b")]
    for i, leaf in enumerate(original, start=1):
        leaf.source_ref = "one-payload"
        leaf.system_metadata.update(_part=i, _parts=len(original))
    harness, model = recursive_harness(original, max_children=2)
    request = make_request(list(reversed(original)))
    built = harness.composer.build(request)
    source_calls = [r for call in model.calls for r in call if r["kind"] == "source"]
    assert [r["text"] for r in source_calls] == [u.content for u in original]
    leaves, parents = read_tree(harness, request, built)
    before_calls, before_writes = len(model.calls), len(harness.builder.calls)
    repeat = harness.composer.replace_in_span(make_request(
        sorted(leaves, key=lambda u: u.id), parents=list(reversed(parents)),
        span=(ORIGIN, ORIGIN + timedelta(minutes=1))))
    assert repeat.complete and not repeat.created_parent_ids
    assert len(model.calls) == before_calls, "storage ordering regenerated unchanged summaries"
    assert len(harness.builder.calls) == before_writes
    assert {u.id: u.content for u in leaves} == {u.id: u.content for u in original}


def test_equal_message_times_use_ingest_order_before_source_id():
    first, second = make_leaf("first"), make_leaf("second")
    first.source_ref, second.source_ref = "z-payload", "a-payload"
    second.temporal.t_ingest += timedelta(seconds=1)
    harness, model = recursive_harness([first, second])
    assert harness.composer.build(make_request([second, first])).complete
    assert [r["text"] for r in model.calls[0]] == [first.content, second.content]


@pytest.mark.parametrize("part,total", [(True, 2), ("1", 2), (0, 2), (3, 2), (1, None)])
def test_invalid_fragment_metadata_does_not_override_stable_source_order(part, total):
    first, second = make_leaf("a"), make_leaf("z")
    for leaf in (first, second):
        leaf.source_ref = "one-payload"
        leaf.system_metadata.update(_part=part, _parts=total)
    first.user_metadata.update(_part=2, _parts=2)
    second.user_metadata.update(_part=1, _parts=2)
    harness, model = recursive_harness([first, second])
    assert harness.composer.build(make_request([second, first])).complete
    assert [r["text"] for r in model.calls[0]] == [first.content, second.content]


def test_changed_speaker_metadata_invalidates_only_the_affected_summary():
    original = [make_leaf(f"u{i}", i) for i in range(4)]
    for leaf in original:
        leaf.segments = [Segment(content="I bought a bike.")]
        leaf.user_metadata["speaker"] = "Alice"
    harness, model = recursive_harness(original, max_children=2)
    request = make_request(original)
    built = harness.composer.build(request)
    leaves, parents = read_tree(harness, request, built)
    original_calls = len(model.calls)
    leaves[0].user_metadata["speaker"] = "Bob"
    harness.builder.update([leaves[0]])
    result = harness.composer.replace_in_span(make_request(
        leaves, parents=parents, span=(ORIGIN, ORIGIN + timedelta(hours=1))))
    assert result.complete and len(result.created_parent_ids) == 2
    assert len(model.calls) == original_calls + 2, "an unrelated subtree was regenerated"
    assert model.calls[original_calls][0]["speaker"] == "Bob"
    assert harness.read(leaves[0].scope, leaves[0].id).content == "I bought a bike."


def test_large_legal_sources_are_reduced_before_pair_merge():
    leaves = [make_leaf(str(i)) for i in range(2)]
    for leaf in leaves:
        leaf.segments = [Segment(content="data " * 600)]
    harness, model = recursive_harness(leaves)
    result = harness.composer.build(make_request(leaves))
    assert result.complete and len(model.calls) == 3
    assert max(harness.composer.summary.call_input_tokens) <= 6000


def test_long_single_source_preserves_all_characters_and_never_overloads_a_call():
    leaf = make_leaf("long")
    leaf.segments = [Segment(content="abcdef " * 4000)]
    harness, model = recursive_harness([leaf], max_children=2)
    result = harness.composer.build(make_request([leaf]))
    source_parts = [r["text"] for call in model.calls for r in call if r["kind"] == "source"]
    assert "".join(source_parts) == leaf.content
    assert result.complete and len(model.calls) > 3
    assert max(harness.composer.summary.call_input_tokens) <= 6000
    assert harness.read(leaf.scope, leaf.id).content == leaf.content


def test_invalid_summary_cannot_write_partial_tree():
    leaves = [make_leaf("a"), make_leaf("b")]
    harness, model = recursive_harness(leaves)
    model.invalid = True
    with pytest.raises(ValidationError, match="failed validation"):
        harness.composer.build(make_request(leaves))
    assert len(model.calls) == 4, "one group repair and one smaller-source repair, then stop"
    assert harness.builder.calls == []


@pytest.mark.parametrize("failure_kind", ["overflow", "incomplete"])
def test_output_overflow_subdivides_and_repeated_request_reuses_whole_subtree(failure_kind):
    class DenseModel(SummaryModel):
        def chat(self, messages, **kwargs):
            records = json.loads(messages[-1].content)
            self.invalid = len(records) > 4
            if self.invalid and failure_kind == "incomplete":
                self.calls.append(records)
                return '{"details":{"0":"unfinished'
            return super().chat(messages, **kwargs)

    original = [make_leaf(f"u{i}", i) for i in range(8)]
    harness, _ = recursive_harness(original)
    model = DenseModel()
    harness.composer.summary.llm = model
    request = make_request(original)
    result = harness.composer.build(request)
    leaves, parents = read_tree(harness, request, result)
    assert result.complete and len(model.calls) == 5
    assert len(parents) == 3 and max(node_height(p) for p in parents) == 2
    assert max(len(p.hierarchy.child_ids) for p in parents) == 4
    assert sum("graphmem_split_input_digest" in p.system_metadata for p in parents) == 1
    validate_tree(leaves + parents)
    assert [u.content for u in leaves] == [u.content for u in original]

    before = len(model.calls), len(harness.builder.calls)
    repeated = harness.composer.replace_in_span(make_request(
        leaves, parents=parents, span=(ORIGIN, ORIGIN + timedelta(hours=1))))
    assert repeated.complete and not repeated.created_parent_ids
    assert before == (len(model.calls), len(harness.builder.calls)), \
        "reused subdivision must not repeat the failed large merge or drop child summaries"

    extra = make_leaf("new", 9)
    harness.builder.build([extra])
    prefix = next(p for p in parents if p.hierarchy.child_ids == [f"u{i}" for i in range(4)])
    appended = harness.composer.replace_in_span(make_request(
        leaves + [extra], parents=parents, span=(ORIGIN, ORIGIN + timedelta(hours=1))))
    assert appended.complete and prefix.id not in appended.replaced_parent_ids


def test_output_overflow_on_two_summary_children_stops_without_rewrapping_forever():
    class ImpossibleParent(SummaryModel):
        def chat(self, messages, **kwargs):
            records = json.loads(messages[-1].content)
            self.invalid = len(records) > 2 or any(r["kind"] == "summary" for r in records)
            return super().chat(messages, **kwargs)

    leaves = [make_leaf(f"u{i}", i) for i in range(4)]
    harness, _ = recursive_harness(leaves)
    model = ImpossibleParent()
    harness.composer.summary.llm = model
    with pytest.raises(ValidationError, match="output view budget"):
        harness.composer.build(make_request(leaves))
    assert len(model.calls) == 6
    assert not harness.builder.calls, "failed recursive preparation must not persist partial nodes"


def test_incomplete_single_source_stops_without_partial_tree_writes():
    class AlwaysIncomplete(SummaryModel):
        def chat(self, messages, **kwargs):
            self.calls.append(json.loads(messages[-1].content))
            return '{"details":{"0":"unfinished'

    harness, _ = recursive_harness([make_leaf("a"), make_leaf("b")])
    model = AlwaysIncomplete()
    harness.composer.summary.llm = model
    with pytest.raises(ValidationError, match="invalid JSON"):
        harness.composer.build(make_request([make_leaf("a"), make_leaf("b")]))
    assert len(model.calls) == 4, "a single source must not be subdivided into empty wrappers"
    assert not harness.builder.calls


def test_malformed_json_does_not_trigger_density_subdivision():
    class BadSyntax(SummaryModel):
        def chat(self, messages, **kwargs):
            self.calls.append(json.loads(messages[-1].content))
            return '{invalid syntax'

    leaves = [make_leaf(f"u{i}", i) for i in range(8)]
    harness, _ = recursive_harness(leaves)
    model = BadSyntax()
    harness.composer.summary.llm = model
    with pytest.raises(ValidationError, match="invalid JSON"):
        harness.composer.build(make_request(leaves))
    assert len(model.calls) == 2
    assert not harness.builder.calls


@pytest.mark.parametrize("allowed_refinements", [0, 2, 3])
def test_output_dense_single_source_refines_losslessly_with_a_depth_limit(allowed_refinements):
    class DenseSource(SummaryModel):
        def __init__(self):
            super().__init__()
            self.completed_parts = []

        def chat(self, messages, **kwargs):
            records = json.loads(messages[-1].content)
            if len(records) == 1 and records[0]["kind"] == "source":
                part = records[0]
                assert part["speaker"] == "Alice" and part["message_time"] == ORIGIN.isoformat()
                if len(part["text"]) > 300:
                    self.calls.append(records)
                    return '{"details":{"0":"unfinished'
                self.completed_parts.append(part["text"])
            return super().chat(messages, **kwargs)

    source = make_leaf("dense")
    source.segments = [Segment(content="abcdefgh " * 228)]
    source.user_metadata["speaker"] = "Alice"
    harness, _ = recursive_harness([source], max_source_refinements=allowed_refinements)
    model = DenseSource()
    harness.composer.summary.llm = model
    if allowed_refinements < 3:
        with pytest.raises(ValidationError):
            harness.composer.build(make_request([source]))
        assert not harness.builder.calls
        assert len(model.calls) <= 2 * (allowed_refinements + 1)
    else:
        result = harness.composer.build(make_request([source]))
        assert result.complete and "".join(model.completed_parts) == source.content
        assert harness.read(source.scope, source.id).content == source.content
        assert max(harness.composer.summary.call_input_tokens) <= 6000


@pytest.mark.parametrize("whitespace_stall", [False, True])
def test_one_repair_distinguishes_grammar_stall_from_normal_truncation(whitespace_stall):
    class FailOnce(SummaryModel):
        def __init__(self):
            super().__init__()
            self.instructions = []

        def chat(self, messages, **kwargs):
            self.instructions.append(messages[0].content)
            if len(self.instructions) == 1:
                return '{"details":{' + (" \n" * 300 if whitespace_stall else '"0":"unfinished')
            return super().chat(messages, **kwargs)

    model = FailOnce()
    composer = RecursiveHierarchyComposer(RecordingIndexBuilder(), model, len,
                                          tokenizer_id="test-counter")
    result = composer.build(make_request([make_leaf("a"), make_leaf("b")]))
    assert result.complete and len(model.instructions) == 2
    assert ("The ONLY details keys" in model.instructions[1]) is whitespace_stall
    assert max(composer.summary.call_input_tokens) <= 6000


def test_replacement_rejects_missing_descendant_before_model_or_write():
    original = [make_leaf(f"u{i}", i) for i in range(8)]
    harness, model = recursive_harness(original, max_children=2)
    request = make_request(original)
    result = harness.composer.build(request)
    leaves, parents = read_tree(harness, request, result)
    before = len(model.calls), len(harness.builder.calls)
    with pytest.raises(ValidationError):
        harness.composer.replace_in_span(make_request(
            leaves[:-1], parents=deepcopy(parents),
            span=(ORIGIN, ORIGIN + timedelta(hours=1))))
    assert before == (len(model.calls), len(harness.builder.calls))


def test_partial_persistence_reports_failure():
    leaves = [make_leaf("a"), make_leaf("b")]
    harness, _ = recursive_harness(leaves)
    harness.builder.fail_at = 2
    result = harness.composer.build(make_request(leaves))
    assert not result.complete and result.created_parent_ids
    assert result.repair_required
    assert harness.read(leaves[0].scope, "a").hierarchy.role is HierarchyRole.SNAPSHOT


def test_complete_legacy_tree_can_migrate_without_changing_sources():
    leaves = [make_leaf(f"u{i}", i) for i in range(4)]
    harness = make_harness(leaves)
    request = make_request(leaves)
    request.options.parent_roles = [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE]
    legacy = harness.composer.build(request)
    current_leaves, old_parents = read_tree(harness, request, legacy)
    composer = RecursiveHierarchyComposer(
        harness.builder, SummaryModel(), len, tokenizer_id="test-counter",
        policy=RecursivePolicy(max_children=2))
    replacement = make_request(current_leaves, parents=old_parents,
                               span=(ORIGIN, ORIGIN + timedelta(hours=1)))
    replacement.options.parent_roles = [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE]
    result = composer.replace_in_span(replacement)
    assert result.complete
    assert set(result.replaced_parent_ids) == set(legacy.created_parent_ids)
    new_leaves, new_parents = read_tree(harness, request, result)
    validate_tree(new_leaves + new_parents)
    assert [u.content for u in new_leaves] == [u.content for u in leaves]
    assert len(new_parents) == 3
