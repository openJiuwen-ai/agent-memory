"""Use existing index alternatives only when bounded relationship navigation stalls."""

from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRole,
    RecallBatch,
    RecallChannel,
    RecallResult,
    RetrievalPipeline,
    ScoredMemoryUnit,
    ScoredUnit,
)
from jiuwen_memory.retrieval import graphmem_relational_navigation as navigation
from jiuwen_memory.retrieval.evidence_packing import EvidencePacker
from jiuwen_memory.retrieval.expander import node_key
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.graph_navigation import GraphEvidenceNavigator, GraphNavigationPolicy
from jiuwen_memory.retrieval.graphmem_evidence_graph import pack_graph
from jiuwen_memory.retrieval.graphmem_query import GraphMemParsedQuery, QueryNeeds
from jiuwen_memory.retrieval.graphmem_relation_plan import EntryProbePool
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.types import DisclosureLevel, RetrievalQuery
from tests.unit.construction.graphmem_fixtures import HOME, tree, unit, world
from tests.unit.retrieval.test_graphmem_detail_navigation import allowed_ids
from tests.unit.retrieval.test_graphmem_relational_navigation import fixture as related_fixture
from tests.unit.retrieval.test_storage_pipelines import SimpleDiscloser, StaticParser

pytestmark = pytest.mark.unit


def fixture(monkeypatch, *, enabled=True, summary_only=False, summary_limit=4):
    commute, commute_raw = tree("commute", "s1", text="Alice commutes for thirty minutes.")
    prepare, prepare_raw = tree("prepare", "s2", text="Alice gets ready for one hour.",
                                raw="Alice takes an hour to get ready before work.")
    extra, extra_raw = tree("extra", "s3", text="Alice packs lunch.")
    units = [commute, commute_raw, prepare, prepare_raw, extra, extra_raw]
    domain, _, _, _ = world(units)
    monkeypatch.setattr(domain, "related_edges", lambda *a, **kw: [])
    index_calls = []

    def indexed(cache, query, target_scope, ids, role, limit, needs, *args, **kwargs):
        index_calls.append((target_scope, tuple(ids), role))
        return {uid: 1.0 for uid in ids[:limit]}, {}, None

    monkeypatch.setattr(navigation, "_index_candidates", indexed)
    query = GraphMemParsedQuery(raw="How much time does Alice spend getting ready and commuting?",
        hierarchy_kind=HierarchyKind.TIME, hierarchy_role=HierarchyRole.TIME_SPAN,
        navigation_needs=QueryNeeds("collection", ("Alice",), ("getting ready", "commuting")))
    policy = GraphNavigationPolicy(strategy="relational", initial_candidate_limit=1,
        summary_candidate_limit=summary_limit, source_candidate_limit=4,
        refill_entry_probes=enabled, include_original=not summary_only)
    pool = EntryProbePool(HOME, (commute.id,), (prepare.id, extra.id))
    return domain, units, query, policy, pool, index_calls


def test_stalled_navigation_reads_one_bounded_batch_and_preserves_real_source_paths(monkeypatch):
    domain, units, query, policy, pool, _ = fixture(monkeypatch)
    plan = navigation.plan_relational(domain, policy, HOME, query,
                                     [ScoredMemoryUnit(units[0], 1)], entry_pool=pool)
    keys = {node_key(c.unit) for c in plan.candidates}
    assert node_key(units[2]) in keys and node_key(units[3]) in keys
    assert node_key(units[4]) not in keys, "Refill is limited by the original entry quota"
    assert plan.diagnostics["entry_refill_added"] == 1
    assert plan.diagnostics["summary_ids_probed"] == 2
    assert plan.diagnostics["neighbors_added"] == 0
    assert all(edge["kind"] == "contains" for edge in plan.links)
    packer = EvidencePacker(len, "characters", mode="sources_first", header_style="compact")
    _, bundle = pack_graph(plan, packer, max_tokens=8000, top_k=32, summary_tokens=0)
    graph = bundle.navigation["evidence_graph"]
    assert "index fallback" in bundle.rendered_context
    assert any(node["reason"].get("selected_by") == "deferred_vector_keyword_match"
               for node in graph["nodes"])
    assert any(edge["kind"] == "contains" for edge in graph["edges"])


def test_disabled_option_keeps_original_plan_and_does_not_read_alternatives(monkeypatch):
    domain, units, query, policy, pool, _ = fixture(monkeypatch, enabled=False)
    old = navigation.plan_relational(domain, policy, HOME, query, [ScoredMemoryUnit(units[0], 1)])
    domain.reads.clear()
    new = navigation.plan_relational(domain, policy, HOME, query,
                                    [ScoredMemoryUnit(units[0], 1)], entry_pool=pool)
    assert old == new
    assert all(units[2].id not in ids for _, ids in domain.reads)


def test_deferred_sources_only_use_remaining_evidence_budget(monkeypatch):
    domain, units, query, policy, pool, _ = fixture(monkeypatch)
    entry = [ScoredMemoryUnit(units[0], 1)]
    primary = navigation.plan_relational(domain, replace(policy, refill_entry_probes=False),
                                         HOME, query, entry)
    with_refill = navigation.plan_relational(domain, policy, HOME, query, entry, entry_pool=pool)
    packer = EvidencePacker(len, "characters", mode="sources_first", header_style="compact")
    _, original = pack_graph(primary, packer, max_tokens=8000, top_k=32, summary_tokens=0)
    _, constrained = pack_graph(with_refill, packer, max_tokens=original.token_count,
                                top_k=32, summary_tokens=0)
    _, extended = pack_graph(with_refill, packer, max_tokens=8000, top_k=32, summary_tokens=0)

    assert constrained.sources == original.sources
    assert constrained.rendered_context == original.rendered_context
    assert {ref.unit_id for ref in extended.sources} == {units[1].id, units[3].id}
    assert extended.navigation["refill_sources_returned"] == 1


def test_deferred_entries_cannot_take_original_tree_source_slots(monkeypatch):
    _, units, query, policy, _, _ = fixture(monkeypatch)
    parents = units[0], units[2]
    leaves = []
    for parent, first in ((parents[0], units[1]), (parents[1], units[3])):
        children = [first, *(unit(f"turn-{i}", raw, scope=first.scope, parent=parent.id)
                            for i, raw in [(2, "Alice checks the bus time."),
                                           (3, "Alice travels to work.")])]
        for child in children:
            child.hierarchy.parent_scope = HOME
        parent.hierarchy.child_ids = [c.id for c in children]
        parent.hierarchy.child_scopes = [c.scope for c in children]
        leaves.extend(children)
    domain, _, _, _ = world([*parents, *leaves])
    pool = EntryProbePool(HOME, (parents[0].id,), (parents[1].id,))
    policy = replace(policy, source_limit=4, source_candidate_limit=4)
    original = navigation.plan_relational(domain, replace(policy, refill_entry_probes=False),
        HOME, query, [ScoredMemoryUnit(parents[0], 1)])
    updated = navigation.plan_relational(domain, policy, HOME, query,
        [ScoredMemoryUnit(parents[0], 1)], entry_pool=pool)
    def sources(plan):
        return {node_key(c.unit) for c in plan.candidates
                if c.unit.hierarchy.role is HierarchyRole.SNAPSHOT}
    assert len(sources(original)) == 3
    assert sources(original) <= sources(updated)
    assert len(sources(updated)) == 4
    assert updated.diagnostics["source_branch_quotas"] == [3, 1]
    assert updated.diagnostics["source_primary_ids"] == 3
    assert updated.diagnostics["source_refill_ids"] == 1


def test_missing_initial_get_consumes_summary_quota_and_is_not_retried(monkeypatch):
    domain, units, query, policy, _, _ = fixture(monkeypatch)
    policy = replace(policy, initial_candidate_limit=2, summary_candidate_limit=3)
    pool = EntryProbePool(HOME, (units[0].id, "missing"), (units[2].id,))
    plan = navigation.plan_relational(domain, policy, HOME, query,
                                     [ScoredMemoryUnit(units[0], 1)], entry_pool=pool)
    assert plan.diagnostics["summary_ids_probed"] == 3
    assert all("missing" not in ids for _, ids in domain.reads)
    assert node_key(units[2]) in {node_key(c.unit) for c in plan.candidates}


def test_summary_only_never_probes_original_indexes_or_bodies(monkeypatch):
    domain, units, query, policy, pool, calls = fixture(monkeypatch, summary_only=True)
    plan = navigation.plan_relational(domain, policy, HOME, query,
                                     [ScoredMemoryUnit(units[0], 1)], entry_pool=pool)
    assert plan.diagnostics["source_candidates"] == 0
    assert all(role is not HierarchyRole.SNAPSHOT for _, _, role in calls)
    assert all(scope == HOME for scope, _ in domain.reads)


def test_first_person_count_uses_one_neighbor_slot_for_a_native_operand(monkeypatch):
    parent, first = tree("visits", "history")
    children = [first, *(unit(f"turn-{i}", "A statement about a visit.", scope=first.scope,
                              parent=parent.id) for i in range(2, 6))]
    for child in children:
        child.hierarchy.parent_scope = HOME
    parent.hierarchy.child_ids = [c.id for c in children]
    parent.hierarchy.child_scopes = [c.scope for c in children]
    domain, _, _, _ = world([parent, *children])

    def recall(scope, parsed, *, channels, recall_limit):
        assert scope == first.scope and recall_limit == 1
        uid = (children[-1].id if channels == [RecallChannel.KEYWORD] else first.id)
        assert uid in allowed_ids(parsed.scalar_filters)
        return RecallResult([RecallBatch(channels[0], "l2", [ScoredUnit(uid, .9)])])

    monkeypatch.setattr(domain, "recall", recall)
    query = GraphMemParsedQuery(
        raw="How many visits did I make?", vector=[1.0], hierarchy_kind=HierarchyKind.TIME,
        hierarchy_role=HierarchyRole.TIME_SPAN,
        navigation_needs=QueryNeeds("collection", (), ("visits",)))
    policy = GraphNavigationPolicy(strategy="relational", use_relations=False,
        source_candidate_limit=3, context_radius=1, prefer_indexed_operands=True)
    seed = [ScoredMemoryUnit(parent, 1)]
    baseline = navigation.plan_relational(
        domain, replace(policy, prefer_indexed_operands=False), HOME, query, seed)
    updated = navigation.plan_relational(domain, policy, HOME, query, seed)
    assert updated.diagnostics["source_branch_quotas"] == [3]
    assert updated.diagnostics["operand_index_slots"] == 1
    assert updated.diagnostics["source_index_ids_returned"] == 2
    assert updated.diagnostics["source_candidates"] == 3
    ids = {node_key(c.unit) for c in updated.candidates}
    assert {node_key(c.unit) for c in baseline.candidates} <= ids
    assert node_key(children[-1]) in ids


def test_cross_scope_pool_is_rejected_before_deferred_reads(monkeypatch):
    domain, units, query, policy, pool, _ = fixture(monkeypatch)
    pool = replace(pool, scope=replace(HOME, user="someone-else"))
    with pytest.raises(ValidationError, match="request scope"):
        navigation.plan_relational(domain, policy, HOME, query,
                                   [ScoredMemoryUnit(units[0], 1)], entry_pool=pool)
    assert not domain.reads


def test_successful_relation_navigation_does_not_add_index_fallback(monkeypatch):
    domain, parents, _, query, policy, _ = related_fixture()
    policy = replace(policy, refill_entry_probes=True)
    pool = EntryProbePool(HOME, (parents[0].id,), ("never-read",))
    plan = navigation.plan_relational(domain, policy, HOME, query,
                                     [ScoredMemoryUnit(parents[0], 1)], entry_pool=pool)
    assert plan.diagnostics["neighbors_added"] > 0
    assert plan.diagnostics["entry_refill_added"] == 0
    assert all("never-read" not in ids for _, ids in domain.reads)


def test_reserved_candidate_uses_one_read_after_a_verified_graph_branch():
    domain, parents, _, query, policy, _ = related_fixture()
    policy = replace(policy, refill_entry_probes=True, reserve_corroborated_entry=True,
                     initial_candidate_limit=1, summary_candidate_limit=3)
    pool = EntryProbePool(HOME, (parents[0].id,), (parents[2].id,), parents[2].id)
    plan = navigation.plan_relational(domain, policy, HOME, query,
                                      [ScoredMemoryUnit(parents[0], 1)], entry_pool=pool)
    assert plan.diagnostics["neighbors_added"] > 0
    assert plan.diagnostics["corroborated_entry_added"]
    assert plan.diagnostics["summary_ids_probed"] == 3
    assert node_key(parents[2]) in {node_key(c.unit) for c in plan.candidates}


@pytest.mark.parametrize(("entities", "facets", "limit", "use_coarse"), [
    (("Alice",), ("getting ready",), 4, True),
    (("Alice", "Bob"), ("getting ready",), 4, False),
    (("Alice",), ("getting ready", "commuting"), 4, False),
    (("Alice",), ("getting ready",), 6, False),
])
def test_coarse_head_uses_the_same_reservation_only_for_one_grounded_demand(
    monkeypatch, entities, facets, limit, use_coarse,
):
    domain, units, query, policy, _, _ = fixture(monkeypatch, summary_limit=limit)
    query = replace(query, navigation_needs=QueryNeeds("collection", entities, facets))
    policy = replace(policy, reserve_corroborated_entry=True, prefer_coarse_keyword_entry=True)
    pool = EntryProbePool(HOME, (units[0].id,), (units[2].id,), units[2].id,
                         coarse_keyword_alternative=units[4].id)
    plan = navigation.plan_relational(domain, policy, HOME, query,
                                      [ScoredMemoryUnit(units[0], 1)], entry_pool=pool)
    chosen = {node_key(item.unit) for item in plan.candidates}
    assert (node_key(units[4]) in chosen) == use_coarse
    assert (node_key(units[2]) in chosen) != use_coarse
    assert bool(plan.diagnostics.get("coarse_keyword_entry_added")) == use_coarse
    assert plan.diagnostics["summary_ids_probed"] == 2


def test_pipeline_preserves_id_alternatives_without_widening_initial_recall(monkeypatch):
    domain, units, query, policy, _, _ = fixture(monkeypatch)
    calls = []

    def recall(scope, parsed, *, channels, recall_limit):
        calls.append(recall_limit)
        return RecallResult(batches=[
            RecallBatch(RecallChannel.VECTOR, "vector_l2", [ScoredUnit(units[0].id, 1)]),
            RecallBatch(RecallChannel.KEYWORD, "keyword_l1", [ScoredUnit(units[2].id, 1)])])

    domain.recall = recall
    parser = StaticParser()
    monkeypatch.setattr(parser, "parse", lambda q: replace(query))
    retriever = PipelineRetriever(parser, RRFFuser(), SimpleDiscloser(), None,
        domain_store=domain, graph_navigator=GraphEvidenceNavigator(domain, policy),
        evidence_packer=EvidencePacker(len, "characters", mode="sources_first"))
    result = retriever.retrieve(HOME, RetrievalQuery(query.raw, top_k=32, max_tokens=8000,
        hierarchy_kind=HierarchyKind.TIME, hierarchy_role=HierarchyRole.TIME_SPAN,
        disclosure=DisclosureLevel.L2))
    assert calls == [1]
    assert domain.reads[0] == (HOME, [units[0].id])
    assert result.evidence_bundle.navigation["entry_refill_added"] == 1


@pytest.mark.parametrize("pipeline", [RetrievalPipeline.RECALL_AND_GET_RANK,
                                     RetrievalPipeline.RETRIEVE])
def test_materializing_pipeline_cannot_claim_id_only_refill(monkeypatch, pipeline):
    domain, _, query, policy, _, _ = fixture(monkeypatch)
    monkeypatch.setattr(domain, "preferred_retrieval_pipeline", lambda: pipeline)
    retriever = PipelineRetriever(StaticParser(), RRFFuser(), SimpleDiscloser(), None,
        domain_store=domain, graph_navigator=GraphEvidenceNavigator(domain, policy),
        evidence_packer=EvidencePacker(len, "characters", mode="sources_first"))
    with pytest.raises(ValidationError, match="ID-first"):
        retriever.retrieve(HOME, RetrievalQuery(query.raw, top_k=32, max_tokens=8000,
            hierarchy_kind=HierarchyKind.TIME, hierarchy_role=HierarchyRole.TIME_SPAN,
            disclosure=DisclosureLevel.L2))
    assert not domain.reads


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_refill_requires_boolean(value):
    with pytest.raises(ValidationError, match="refill_entry_probes"):
        GraphNavigationPolicy(strategy="relational", refill_entry_probes=value)


def test_refill_is_explicit_and_only_for_relational_navigation():
    assert not GraphNavigationPolicy().refill_entry_probes
    with pytest.raises(ValidationError, match="supports relational"):
        GraphNavigationPolicy(strategy="legacy", refill_entry_probes=True)


def test_temporal_keyword_entry_uses_existing_reservation_for_one_entity_and_event(monkeypatch):
    domain, units, query, policy, pool, _ = fixture(monkeypatch)
    query = replace(query, raw="When did Alice get ready for work?",
                    navigation_needs=QueryNeeds("temporal", ("Alice",), ("get ready for work",)))
    policy = replace(policy, reserve_corroborated_entry=True, prefer_temporal_keyword_entry=True)
    pool = replace(pool, alternative_ids=(units[4].id,), temporal_keyword_alternative=units[2].id)
    seed = [ScoredMemoryUnit(units[0], 1)]
    plan = navigation.plan_relational(domain, policy, HOME, query, seed, entry_pool=pool)

    assert plan.diagnostics["temporal_keyword_entry_added"]
    assert plan.diagnostics["summary_ids_probed"] == 2
    assert node_key(units[3]) in {node_key(item.unit) for item in plan.candidates}
    ordinary = replace(query, navigation_needs=replace(query.navigation_needs, mode="fact"))
    control = navigation.plan_relational(domain, policy, HOME, ordinary, seed, entry_pool=pool)
    assert "temporal_keyword_entry_reserved" not in control.diagnostics
    assert node_key(units[4]) in {node_key(item.unit) for item in control.candidates}
