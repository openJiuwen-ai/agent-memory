"""Exact index spans guide bounded child lookup, without adding evidence authority."""

import hashlib
import json
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.graphmem_structure import DETAIL_SPANS_KEY
from jiuwen_memory.common.type_def import (
    Chunk,
    HierarchyRole,
    MemoryUnit,
    RecallBatch,
    RecallChannel,
    RecallResult,
    ScoredMemoryUnit,
    ScoredUnit,
    Segment,
)
from jiuwen_memory.common.type_def.retrieval import ContentMatch
from jiuwen_memory.construction.index_builder_impl._index_ops import (
    chunk_location_metadata,
)
from jiuwen_memory.retrieval.graph_navigation import GraphNavigationPolicy
from jiuwen_memory.retrieval.graphmem_chunk_hints import (
    attach_content_matches,
    child_chunk_hints,
    collect_content_matches,
)
from jiuwen_memory.retrieval.graphmem_relational_navigation import plan_relational
from jiuwen_memory.retrieval.graphmem_request_reads import _RequestReads
from jiuwen_memory.storage.domain_store_impl.unit_aggregation import aggregate_to_units
from jiuwen_memory.storage.types import ScoredID, VectorRecord
from tests.unit.construction.graphmem_fixtures import HOME
from tests.unit.retrieval.test_graphmem_detail_navigation import allowed_ids, fixture

pytestmark = pytest.mark.unit


def match_for(parent, position=3):
    start, end = json.loads(parent.system_metadata[DETAIL_SPANS_KEY])["spans"][position]
    offset = len(parent.segments[0].content) + 1
    return ContentMatch("index-record", position, offset + start, offset + end,
                        hashlib.sha256(parent.content.encode()).hexdigest(), .8)


@pytest.mark.parametrize("mode", ["normal", "empty_index", "bad_backlink", "stale_content",
                                  "stale_map", "summary_only", "disabled"])
def test_chunk_reference_keeps_index_scope_link_read_and_summary_only_boundaries(monkeypatch, mode):
    domain, parent, children, query, policy = fixture()
    query = replace(query, vector=[1.0, 0.0])
    policy = replace(policy, detail_source_hints=False, chunk_source_hints=mode != "disabled",
                     include_original=mode != "summary_only")
    match = match_for(parent)
    if mode == "bad_backlink":
        children[3].hierarchy.parent_id = "different-parent"
        domain.update(children[3].scope, [children[3]])
    if mode == "stale_content":
        parent.segments[1].content += " changed"
        domain.update(HOME, [parent])
    if mode == "stale_map":
        data = json.loads(parent.system_metadata[DETAIL_SPANS_KEY])
        data["binding"] = "stale"
        parent.system_metadata[DETAIL_SPANS_KEY] = json.dumps(data)
        domain.update(HOME, [parent])
    calls = []

    def recall(scope, parsed, *, channels, recall_limit):
        ids = allowed_ids(parsed.scalar_filters)
        calls.append((scope, ids, recall_limit))
        chosen = [] if mode == "empty_index" else sorted(ids)[:recall_limit]
        return RecallResult([RecallBatch(channels[0], "l2", [ScoredUnit(uid, .8)
                                                               for uid in chosen])])

    monkeypatch.setattr(domain, "recall", recall)
    domain.reads.clear()
    plan = plan_relational(domain, policy, HOME, query,
                          [ScoredMemoryUnit(parent, 1, content_match=match)])
    sources = [c for c in plan.candidates if c.unit.hierarchy.role is HierarchyRole.SNAPSHOT]
    assert len(sources) <= 2
    assert sum(len(ids) for scope, ids in domain.reads if scope.session) <= 2
    assert plan.diagnostics["event_source_candidates"] == 0
    if mode == "normal":
        assert {c.unit.id for c in sources} == {children[0].id, children[3].id}
        assert calls[0][1] == parent.hierarchy.child_ids
        assert calls[-1][1] == [children[3].id]
        assert plan.diagnostics["chunk_source_candidates"] == 1
        reason = next(e["reason"] for e in plan.links if e["target"][-1] == children[3].id)
        assert reason["event_references"] == []
        assert reason["detail_references"][0]["chunk_seq"] == 3
        assert "clauses" not in reason["detail_references"][0]
    else:
        assert children[3].id not in {c.unit.id for c in sources}
        if mode == "summary_only":
            assert not calls and not any(scope.session for scope, _ in domain.reads)


@pytest.mark.parametrize("replacement", [
    {"seq": True}, {"start": -1}, {"end": 100000}, {"score": float("nan")},
    {"score": True}, {"content_sha256": "0" * 64}, {"record_id": None},
])
def test_location_must_match_current_parent_content(replacement):
    _, parent, children, _, _ = fixture()
    match = replace(match_for(parent), **replacement)
    assert not child_chunk_hints(parent, match, children[0].scope,
                                 parent.hierarchy.child_ids, HierarchyRole.SNAPSHOT)


def test_location_cannot_cross_child_scope_or_role_or_claim_unrelated_child():
    _, parent, children, _, _ = fixture()
    match = match_for(parent)
    assert not child_chunk_hints(parent, match, HOME, parent.hierarchy.child_ids,
                                 HierarchyRole.SNAPSHOT)
    assert not child_chunk_hints(parent, match, children[0].scope, parent.hierarchy.child_ids,
                                 HierarchyRole.TIME_SPAN)
    assert not child_chunk_hints(parent, match, children[0].scope, [children[0].id],
                                 HierarchyRole.SNAPSHOT)


def test_indexer_only_publishes_exact_content_offsets():
    unit = MemoryUnit(segments=[Segment(content="甲乙 abcdef")])
    valid = Chunk(seq=0, text="abc", start=3, end=6)
    assert chunk_location_metadata(unit, valid) == {"content_start": 3, "content_end": 6}
    for invalid in [replace(valid, text="projection"), replace(valid, start=True),
                    replace(valid, end=999), replace(valid, seq=-1)]:
        assert not chunk_location_metadata(unit, invalid)


def test_maxp_retains_only_winning_native_l2_span_and_missing_metadata_falls_back():
    meta = {"unit_id": "same", "content_layer": "l2", "seq": 0,
            "content_start": 0, "content_end": 10, "indexed_text_sha256": "a" * 64}
    records = [VectorRecord("first", [], meta), VectorRecord("second", [],
        {**meta, "seq": 1, "content_start": 10, "content_end": 20})]
    hits = [ScoredID("first", .4), ScoredID("second", .8)]
    actual = aggregate_to_units(hits, records, RecallChannel.VECTOR)
    assert len(actual) == 1 and actual[0].score == .8
    assert actual[0].content_match.seq == 1
    assert aggregate_to_units(hits, records, RecallChannel.KEYWORD)[0].content_match is None
    records[1].metadata = {"unit_id": "same"}
    assert aggregate_to_units(hits, records, RecallChannel.VECTOR)[0].content_match is None


def test_initial_fusion_keeps_l2_location_without_changing_fused_score_or_order():
    _, parent, _, _, _ = fixture()
    match = match_for(parent)
    batches = [RecallBatch(RecallChannel.VECTOR, "vector_l2",
                          [ScoredUnit(parent.id, .8, content_match=match)]),
               RecallBatch(RecallChannel.VECTOR, "vector_l1",
                           [ScoredUnit(parent.id, .99, content_match=replace(match, seq=99))])]
    fused = [ScoredMemoryUnit(parent, .035, RecallChannel.KEYWORD)]
    result = attach_content_matches(fused, batches)
    assert result[0].content_match == match
    assert replace(result[0], content_match=None) == fused[0]
    assert not collect_content_matches(batches[1:])


def test_request_cache_keeps_matches_separate_for_identical_ids_in_different_scopes(monkeypatch):
    domain, parent, _, query, policy = fixture()
    policy = replace(policy, detail_source_hints=False, chunk_source_hints=True)
    match = match_for(parent)
    other = replace(HOME, session="other")
    monkeypatch.setattr(domain, "recall", lambda *args, **kwargs: RecallResult([
        RecallBatch(RecallChannel.VECTOR, "vector_l2",
                    [ScoredUnit(parent.id, .8, content_match=match)])]))
    cache = _RequestReads(domain, policy, [])
    cache.recall(other, query, channels=[RecallChannel.VECTOR], recall_limit=1)
    assert cache.content_matches[cache.key(other, parent.id)] == match
    assert cache.key(HOME, parent.id) not in cache.content_matches


def test_chunk_hint_option_is_explicit_and_boolean():
    assert not GraphNavigationPolicy().chunk_source_hints
    with pytest.raises(ValidationError):
        GraphNavigationPolicy(strategy="relational", chunk_source_hints=1)


@pytest.mark.parametrize("adjacency", [False, True])
def test_full_original_branch_keeps_sources_and_links_without_chunk_queries(monkeypatch, adjacency):
    domain, parent, children, query, policy = fixture()
    query = replace(query, vector=[1.0, 0.0])
    policy = replace(policy, detail_source_hints=False, chunk_source_hints=True,
                     context_radius=1 if adjacency else 0)
    calls = []

    def recall(scope, parsed, *, channels, recall_limit):
        ids = allowed_ids(parsed.scalar_filters)
        calls.append((channels, ids, recall_limit))
        # The full branch gets distinct vector/keyword hits. With adjacency,
        # one index hit plus its adjacent turn fills the same two-source quota.
        index = 0 if channels[0] is RecallChannel.VECTOR else 1
        assert children[index].id in ids
        return RecallResult([RecallBatch(channels[0], "l2", [
            ScoredUnit(children[index].id, .8),
        ])])

    monkeypatch.setattr(domain, "recall", recall)
    seeds = [ScoredMemoryUnit(parent, 1, content_match=match_for(parent))]
    control = plan_relational(domain, replace(policy, chunk_source_hints=False), HOME, query, seeds)
    before = list(calls)
    calls.clear()
    candidate = plan_relational(domain, policy, HOME, query, seeds)
    assert calls == before
    assert candidate.links == control.links
    assert candidate.reasons == control.reasons
    assert [c.unit.id for c in candidate.candidates] == [c.unit.id for c in control.candidates]
    assert candidate.diagnostics["chunk_source_candidates"] == 0
