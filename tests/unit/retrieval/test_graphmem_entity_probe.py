"""Query-ranked entity links may be explored without claiming scene coverage."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.graph_memory import memory_fingerprint
from jiuwen_memory.common.graphmem_semantics import (
    FEATURES_KEY,
    EntityLabel,
    EventTime,
    GraphSemantics,
    SceneEvent,
)
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, ScoredMemoryUnit
from jiuwen_memory.retrieval import graphmem_relational_navigation as navigation
from jiuwen_memory.retrieval.evidence_packing import EvidencePacker
from jiuwen_memory.retrieval.graph_navigation import GraphNavigationPolicy
from jiuwen_memory.retrieval.graphmem_evidence_graph import pack_graph
from jiuwen_memory.retrieval.graphmem_query import GraphMemParsedQuery, QueryNeeds
from jiuwen_memory.retrieval.graphmem_relation_plan import EntryProbePool, compile_program
from jiuwen_memory.retrieval.graphmem_relation_selection import relation_reason
from tests.unit.construction.graphmem_fixtures import HOME, tree, world

pytestmark = pytest.mark.unit


def fixture(monkeypatch):
    pairs = [tree("seed", "s1", text="Kai visited Cedar Park.", raw="Kai walked two miles."),
             tree("route", "s2", text="Kai walked the mountain loop.",
                  raw="Kai walked three miles."),
             tree("next", "s3", text="Kai visited the canyon.", raw="Kai walked four miles.")]
    parents, leaves = map(list, zip(*pairs))
    kai = EntityLabel("Kai", "person")
    for parent in parents:
        parent.system_metadata[FEATURES_KEY] = GraphSemantics((kai,), ()).encode()
    domain, _, _, _ = world([*parents, *leaves])
    edges = [SimpleNamespace(id=f"edge-{i}", source=a.id, target=b.id, properties={
        "attributes": ["entity"], "shared_entities": [kai.key], "score": 0.8,
        "fingerprints": {a.id: memory_fingerprint(a), b.id: memory_fingerprint(b)}})
        for i, (a, b) in enumerate(zip(parents, parents[1:]))]
    monkeypatch.setattr(domain, "related_edges", lambda scope, ids, **kw:
                        [e for e in edges if e.source in ids or e.target in ids])

    def indexed(cache, query, target_scope, ids, role, limit, needs, *args, **kwargs):
        ids = list(ids)[:limit]
        return {uid: 1 / (1 + i) for i, uid in enumerate(ids)}, {
            uid: 0.7 for uid in ids}, ids[0] if ids else None

    monkeypatch.setattr(navigation, "_index_candidates", indexed)
    query = GraphMemParsedQuery(raw="What were Kai's outdoor distances?",
        hierarchy_kind=HierarchyKind.TIME, hierarchy_role=HierarchyRole.TIME_SPAN,
        navigation_needs=QueryNeeds("collection", ("Kai",), ("outdoor distances",)))
    policy = GraphNavigationPolicy(strategy="relational", seed_count=1, max_hops=3,
        query_guided_entity_probe=True, initial_candidate_limit=1, summary_candidate_limit=3,
        source_candidate_limit=4, source_limit=2, max_neighbors=4, max_reads=12)
    return domain, parents, query, policy, edges


def test_entity_probe_is_explicit_and_shares_the_existing_one_probe_allowance(monkeypatch):
    domain, parents, query, policy, _ = fixture(monkeypatch)
    seed = [ScoredMemoryUnit(parents[0], 1)]
    old = navigation.plan_relational(domain, replace(policy, query_guided_entity_probe=False),
                                    HOME, query, seed)
    assert old.diagnostics["neighbors_added"] == 0
    plan = navigation.plan_relational(domain, policy, HOME, query, seed)
    assert plan.diagnostics["neighbors_added"] == plan.diagnostics["semantic_probes_added"] == 1
    assert parents[1].id in {c.unit.id for c in plan.candidates}
    assert parents[2].id not in {c.unit.id for c in plan.candidates}
    links = [e for e in plan.links if e["kind"] == "related"]
    assert len(links) == 1
    reason = links[0]["reason"]
    assert reason["probe_relation"] == "entity"
    assert reason["shared_relation_entities"] == ["Kai"]
    assert "scene_clauses" not in reason and "entities" not in reason
    assert reason["clause_transition"] == {"from": [], "to": []}
    assert plan.diagnostics["summary_ids_probed"] <= policy.summary_candidate_limit
    assert plan.diagnostics["source_candidates"] <= policy.source_candidate_limit
    packer = EvidencePacker(len, "characters", mode="sources_first", header_style="compact")
    _, bundle = pack_graph(plan, packer, max_tokens=8000, top_k=16, summary_tokens=1000)
    assert "query_probe(entity)" in bundle.rendered_context
    assert bundle.token_count <= 8000


@pytest.mark.parametrize("qualified", [False, True])
def test_only_qualified_relationship_stops_bounded_index_refill(monkeypatch, qualified):
    domain, parents, query, policy, _ = fixture(monkeypatch)
    policy = replace(policy, refill_entry_probes=True, max_hops=1)
    if qualified:
        query.navigation_needs = replace(query.navigation_needs, facets=("mountain loop",))
    pool = EntryProbePool(HOME, (parents[0].id,), (parents[2].id,))
    plan = navigation.plan_relational(domain, policy, HOME, query,
        [ScoredMemoryUnit(parents[0], 1)], entry_pool=pool)
    assert plan.diagnostics["neighbors_added"] == 1
    assert plan.diagnostics["semantic_probes_added"] == int(not qualified)
    assert plan.diagnostics["entry_refill_added"] == int(not qualified)
    assert (parents[2].id in {c.unit.id for c in plan.candidates}) is (not qualified)
    related = [link for link in plan.links if link["kind"] == "related"]
    assert len(related) == 1, "An index fallback must not be reported as a relationship hit"
    assert plan.diagnostics["summary_ids_probed"] <= policy.summary_candidate_limit
    assert plan.diagnostics["source_candidates"] <= policy.source_candidate_limit


@pytest.mark.parametrize("summary_only", [False, True])
def test_probes_cannot_bypass_summary_limits_or_pure_summary_mode(monkeypatch, summary_only):
    domain, parents, query, policy, _ = fixture(monkeypatch)
    original_get = domain.get

    def get(scope, ids, **options):
        assert not summary_only or not scope.session
        return original_get(scope, ids, **options)

    monkeypatch.setattr(domain, "get", get)
    policy = replace(policy, include_original=not summary_only, summary_candidate_limit=1)
    plan = navigation.plan_relational(domain, policy, HOME, query,
                                     [ScoredMemoryUnit(parents[0], 1)])
    assert plan.diagnostics["neighbors_added"] == 0
    assert plan.diagnostics["summary_ids_probed"] == 1


def test_stale_entity_edge_is_not_an_exploration_route(monkeypatch):
    domain, parents, query, policy, edges = fixture(monkeypatch)
    edges[0].properties["fingerprints"][parents[1].id] = "outdated"
    plan = navigation.plan_relational(domain, policy, HOME, query,
                                     [ScoredMemoryUnit(parents[0], 1)])
    assert plan.diagnostics["neighbors_added"] == 0
    assert plan.diagnostics["stale_edges"] == 1


@pytest.mark.parametrize("case", ["disabled", "zero_similarity", "missing_shared",
                                  "other_named_entity", "target_not_shared", "no_allowance"])
def test_unverified_or_irrelevant_entity_context_cannot_be_probed(monkeypatch, case):
    _, parents, query, _, edges = fixture(monkeypatch)
    kwargs = {"query_similarity": 0.7, "allow_semantic_probe": True, "allow_entity_probe": True}
    if case == "disabled":
        kwargs["allow_entity_probe"] = False
    elif case == "zero_similarity":
        kwargs["query_similarity"] = 0
    elif case == "missing_shared":
        edges[0].properties["shared_entities"] = []
    elif case == "other_named_entity":
        query.navigation_needs = replace(query.navigation_needs, entities=("Morgan",))
    elif case == "target_not_shared":
        parents[1].system_metadata[FEATURES_KEY] = GraphSemantics(
            (EntityLabel("Morgan", "person"),), ()).encode()
    else:
        kwargs["allow_semantic_probe"] = False
    assert not relation_reason(parents[0], parents[1], edges[0], query.navigation_needs,
                               compile_program(query), **kwargs)


def test_unknown_query_identity_can_follow_verified_shared_context(monkeypatch):
    _, parents, query, _, edges = fixture(monkeypatch)
    needs = replace(query.navigation_needs, entities=())
    reason = relation_reason(parents[0], parents[1], edges[0], needs,
        compile_program(query, needs), 0.7, allow_semantic_probe=True, allow_entity_probe=True)
    assert reason["probe_relation"] == "entity" and "scene_clauses" not in reason


def test_known_outside_event_window_remains_rejected(monkeypatch):
    _, parents, query, _, edges = fixture(monkeypatch)
    kai = EntityLabel("Kai", "person")
    parents[1].system_metadata[FEATURES_KEY] = GraphSemantics((kai,), (
        SceneEvent("outdoor distances", (kai.key,), "completed",
                   EventTime("2022-01-01", "2022-01-02", "day", "occurrence"), (0,)),
    )).encode()
    needs = replace(query.navigation_needs, time_from="2023-01-01", time_to="2023-02-01")
    assert not relation_reason(parents[0], parents[1], edges[0], needs,
        compile_program(query, needs), 0.7, allow_semantic_probe=True, allow_entity_probe=True)


@pytest.mark.parametrize("value", [1, "true", None])
def test_entity_probe_is_a_strict_boolean(value):
    with pytest.raises(ValidationError, match="query_guided_entity_probe must be bool"):
        GraphNavigationPolicy(strategy="relational", query_guided_entity_probe=value)


def test_entity_probe_requires_relational_navigation():
    with pytest.raises(ValidationError, match="supports relational"):
        GraphNavigationPolicy(strategy="legacy", query_guided_entity_probe=True)
