"""A single semantic exploration uses an eligible new endpoint, with unchanged reads."""

from types import SimpleNamespace

import pytest

from jiuwen_memory.common.graph_memory import memory_fingerprint
from jiuwen_memory.common.graphmem_semantics import FEATURES_KEY, EntityLabel, GraphSemantics
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, ScoredMemoryUnit
from jiuwen_memory.retrieval import graphmem_relational_navigation as navigation
from jiuwen_memory.retrieval.graph_navigation import GraphNavigationPolicy
from jiuwen_memory.retrieval.graphmem_query import GraphMemParsedQuery, QueryNeeds
from tests.unit.construction.graphmem_fixtures import HOME, tree, world

pytestmark = pytest.mark.unit


def fixture(
    monkeypatch,
    *,
    known=False,
    wrong_entity=False,
    stale=False,
    empty=False,
    detail_best=False,
    zero_new=False,
    mode="fact",
    facets=("riverside picnic",),
):
    parents = [
        tree(uid, uid, text="Alice celebrated a milestone.")[0]
        for uid in ("seed", "weak", "strong", "known")
    ]
    for parent in parents:
        name = "Bob" if wrong_entity and parent.id == "strong" else "Alice"
        parent.segments[0].content = f"{name} celebrated a milestone."
        parent.system_metadata[FEATURES_KEY] = GraphSemantics(
            (EntityLabel(name, "person"),), ()
        ).encode()
    domain, _, _, _ = world(parents)
    edges = [
        SimpleNamespace(
            id="edge-" + p.id,
            source="seed",
            target=p.id,
            properties={
                "attributes": ["semantic"],
                "score": 0.8,
                "fingerprints": {
                    "seed": memory_fingerprint(parents[0]),
                    p.id: "stale" if stale and p.id == "strong" else memory_fingerprint(p),
                },
            },
        )
        for p in parents[1:]
    ]
    monkeypatch.setattr(domain, "related_edges", lambda *args, **kwargs: edges)
    calls = []

    def indexed(cache, query, target_scope, ids, role, limit, needs, *args, **kwargs):
        assert target_scope == HOME
        calls.append((list(ids), limit))
        if empty:
            return {}, {}, None
        ranks = (
            {"known": 0.1, "weak": 0.09, "strong": 0.08}
            if known
            else {"weak": 0.1, "strong": 0.09, "known": 0.08}
        )
        if wrong_entity or stale:
            ranks = (
                {"known": 0.1, "strong": 0.09, "weak": 0.08}
                if known
                else {"strong": 0.1, "weak": 0.09, "known": 0.08}
            )
        return (
            ranks,
            {"known": 0.95 if known else 0.1, "weak": 0 if zero_new else 0.3, "strong": 0.8},
            "strong" if detail_best else None,
        )

    monkeypatch.setattr(navigation, "_index_candidates", indexed)
    query = GraphMemParsedQuery(
        raw="What did Alice bring to the riverside picnic?",
        hierarchy_kind=HierarchyKind.TIME,
        hierarchy_role=HierarchyRole.TIME_SPAN,
        navigation_needs=QueryNeeds(mode=mode, entities=("Alice",), facets=facets),
    )
    policy = GraphNavigationPolicy(
        strategy="relational",
        seed_count=2,
        max_hops=1,
        max_neighbors=2,
        branch_width=2,
        include_original=False,
        initial_candidate_limit=2,
        summary_candidate_limit=4,
    )
    entries = [ScoredMemoryUnit(parents[0], 1)]
    if known:
        entries.append(ScoredMemoryUnit(parents[3], 0.9))
    plan = navigation.plan_relational(domain, policy, HOME, query, entries)
    return plan, domain, calls


def test_unvisited_probe_retains_original_index_ranking(monkeypatch):
    plan, _, calls = fixture(monkeypatch)
    assert {c.unit.id for c in plan.candidates} == {"seed", "weak"}
    assert len(calls) == 1
    assert plan.diagnostics["semantic_probes_added"] == 1
    reason = next(e["reason"] for e in plan.links if e["kind"] == "related")
    assert reason["semantic_probe"] and reason["query_scene_similarity"] == 0.3
    assert not reason["clause_transition"]["to"]
    assert "entities" not in reason and "scene_clauses" not in reason


def test_detailed_vector_head_is_checked_within_the_existing_probe_budget(monkeypatch):
    plan, _, calls = fixture(monkeypatch, detail_best=True)
    assert {c.unit.id for c in plan.candidates} == {"seed", "strong"}
    assert len(calls) == 1
    assert plan.diagnostics["summary_ids_probed"] <= 4
    assert plan.diagnostics["semantic_probes_added"] == 1
    reason = next(e["reason"] for e in plan.links if e["kind"] == "related")
    assert reason["semantic_probe"] and not reason["clause_transition"]["to"]


@pytest.mark.parametrize("rejected", ["wrong_entity", "stale"])
def test_detailed_head_does_not_override_entity_or_version_checks(monkeypatch, rejected):
    plan, _, _ = fixture(monkeypatch, detail_best=True, **{rejected: True})
    assert {c.unit.id for c in plan.candidates} == {"seed"}
    assert not plan.diagnostics["semantic_probes_added"]


def test_collection_can_probe_new_endpoint_after_selected_head(monkeypatch):
    plan, _, _ = fixture(monkeypatch, known=True, mode="collection")
    assert {c.unit.id for c in plan.candidates} == {"seed", "known", "weak"}
    assert plan.diagnostics["semantic_probes_added"] == plan.diagnostics["neighbors_added"] == 1
    assert plan.diagnostics["summary_ids_probed"] <= 4
    reason = next(e["reason"] for e in plan.links if e["kind"] == "related")
    assert reason["semantic_probe"] and not reason["clause_transition"]["to"]
    assert plan.diagnostics["summary_ids_probed"] <= 4


@pytest.mark.parametrize("reason", ["wrong_entity", "stale"])
def test_higher_similarity_does_not_override_entity_or_version_rejection(monkeypatch, reason):
    plan, _, _ = fixture(monkeypatch, **{reason: True})
    assert {c.unit.id for c in plan.candidates} == {"seed"}
    assert plan.diagnostics["semantic_probes_added"] == 0


def test_empty_index_does_not_materialize_any_probe(monkeypatch):
    plan, domain, _ = fixture(monkeypatch, empty=True)
    assert {c.unit.id for c in plan.candidates} == {"seed"}
    assert not domain.reads and not plan.diagnostics["semantic_probes_added"]


def staged_fixture(monkeypatch, *, dead_end=False, max_hops=2):
    parents = {uid: tree(uid, uid)[0] for uid in ("seed", "bridge", "goal", "noise1", "noise2")}
    for uid, parent in parents.items():
        relevant = not uid.startswith("noise")
        name = "Alice" if relevant else "Bob"
        parent.segments[0].content = (
            "Alice brought bread to the riverside picnic."
            if relevant
            else "Bob climbed a mountain."
        )
        parent.system_metadata[FEATURES_KEY] = GraphSemantics(
            (EntityLabel(name, "person"),), ()
        ).encode()
    edges = [
        SimpleNamespace(
            id=f"{a}-{b}",
            source=a,
            target=b,
            properties={
                "attributes": ["semantic"],
                "score": 0.8,
                "fingerprints": {
                    a: memory_fingerprint(parents[a]),
                    b: memory_fingerprint(parents[b]),
                },
            },
        )
        for a, b in (("seed", "bridge"), ("seed", "noise1"), ("seed", "noise2"), ("bridge", "goal"))
    ]
    domain, _, _, _ = world(list(parents.values()))
    monkeypatch.setattr(
        domain,
        "related_edges",
        lambda scope, ids, **kwargs: [
            edge for edge in edges if edge.source in ids or edge.target in ids
        ],
    )
    ranks = {
        uid: 0.9 - i * 0.1
        for i, uid in enumerate(
            ("noise1", "noise2", "bridge", "goal")
            if dead_end
            else ("bridge", "noise1", "noise2", "goal")
        )
    }

    def indexed(cache, query, target_scope, ids, role, limit, needs, *args, **kwargs):
        return {uid: ranks[uid] for uid in ids if uid in ranks}, {}, None

    monkeypatch.setattr(navigation, "_index_candidates", indexed)
    query = GraphMemParsedQuery(
        raw="What did Alice bring to the riverside picnic?",
        hierarchy_kind=HierarchyKind.TIME,
        hierarchy_role=HierarchyRole.TIME_SPAN,
        navigation_needs=QueryNeeds(entities=("Alice",), facets=("riverside picnic",)),
    )
    policy = GraphNavigationPolicy(
        strategy="relational",
        seed_count=1,
        max_hops=max_hops,
        max_neighbors=3,
        branch_width=2,
        include_original=False,
        ancestor_hops=0,
        initial_candidate_limit=1,
        summary_candidate_limit=4,
    )
    plan = navigation.plan_relational(
        domain, policy, HOME, query, [ScoredMemoryUnit(parents["seed"], 1)]
    )
    return plan


def test_later_breadth_first_hop_retains_a_read_slot_after_rejected_neighbor(monkeypatch):
    plan = staged_fixture(monkeypatch)
    assert {c.unit.id for c in plan.candidates} == {"seed", "bridge", "goal"}
    assert plan.diagnostics["summary_ids_probed"] == 4
    assert plan.diagnostics["neighbors_added"] == 2
    assert not plan.diagnostics["semantic_probes_added"]
    assert [(e["source"][-1], e["target"][-1]) for e in plan.links] == [
        ("seed", "bridge"),
        ("bridge", "goal"),
    ]
    assert not plan.diagnostics["relation_probe_stages"][0]["dead_end_fallback"]


def test_dead_end_uses_reserved_slots_on_remaining_same_hop_candidates(monkeypatch):
    plan = staged_fixture(monkeypatch, dead_end=True)
    assert {c.unit.id for c in plan.candidates} == {"seed", "bridge"}
    assert plan.diagnostics["summary_ids_probed"] == 4
    assert plan.diagnostics["relation_probe_stages"][0]["dead_end_fallback"]
    assert not plan.diagnostics["semantic_probes_added"]


def test_single_hop_never_traverses_second_hop_or_reserves_unused_budget(monkeypatch):
    plan = staged_fixture(monkeypatch, max_hops=1)
    assert {c.unit.id for c in plan.candidates} == {"seed", "bridge"}
    assert plan.diagnostics["summary_ids_probed"] == 4
    assert len(plan.diagnostics["relation_probe_stages"]) == 1
    assert not plan.diagnostics["relation_probe_stages"][0]["dead_end_fallback"]


@pytest.mark.parametrize("rejected", ["wrong_entity", "stale", "zero_new"])
def test_new_head_after_known_endpoint_still_obeys_admission_checks(monkeypatch, rejected):
    plan, _, calls = fixture(monkeypatch, known=True, mode="collection", **{rejected: True})
    assert {c.unit.id for c in plan.candidates} == {"seed", "known"}
    assert not plan.diagnostics["semantic_probes_added"]
    assert plan.diagnostics["summary_ids_probed"] <= 4
    assert len(calls) == 1


@pytest.mark.parametrize(
    "mode,facets",
    [
        ("fact", ("riverside picnic",)),
        ("state", ("riverside picnic",)),
        ("temporal", ("riverside picnic",)),
        ("comparison", ("riverside picnic", "school picnic")),
        ("multi_step", ("riverside picnic", "school picnic")),
        ("fact", ("riverside picnic", "school picnic")),
    ],
)
def test_other_operators_keep_original_head_eligibility(monkeypatch, mode, facets):
    plan, _, calls = fixture(monkeypatch, known=True, mode=mode, facets=facets)
    assert {c.unit.id for c in plan.candidates} == {"seed", "known"}
    assert not plan.diagnostics["semantic_probes_added"]
    assert plan.diagnostics["summary_ids_probed"] <= 4
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("stale", "expected"),
    [
        (False, "goal"),
        (True, None),
    ],
)
def test_uncovered_clause_uses_one_existing_probe_slot_and_rechecks_version(
    monkeypatch, stale, expected
):
    from jiuwen_memory.common.graphmem_semantics import EventTime, SceneEvent

    texts = {
        "seed": "birthday picnic",
        "head": "birthday picnic",
        "lower": "birthday picnic",
        "goal": "bought antique clock",
    }
    alice = EntityLabel("Alice", "person")
    nodes = {}
    for name, scene in texts.items():
        node = tree(name, name, text=f"Alice {scene}.")[0]
        node.system_metadata[FEATURES_KEY] = GraphSemantics(
            (alice,),
            (SceneEvent(scene, (alice.key,), "completed", EventTime(), (0,)),),
        ).encode()
        nodes[name] = node
    edges = [
        SimpleNamespace(
            id=f"edge-{uid}",
            source="seed",
            target=uid,
            properties={
                "attributes": ["semantic"],
                "score": 0.8,
                "fingerprints": {
                    "seed": memory_fingerprint(nodes["seed"]),
                    uid: ("stale" if stale and uid == "goal" else memory_fingerprint(nodes[uid])),
                },
            },
        )
        for uid in ("head", "lower", "goal")
    ]
    domain, _, _, _ = world(list(nodes.values()))
    monkeypatch.setattr(domain, "related_edges", lambda *args, **kwargs: edges)

    def indexed(cache, query, scope, ids, role, limit, needs, *args, entry_events=None):
        if entry_events is not None:
            entry_events.update({uid: nodes[uid].system_metadata[FEATURES_KEY] for uid in ids})
        return {"head": 0.9, "lower": 0.8, "goal": 0.7}, {}, None

    monkeypatch.setattr(navigation, "_index_candidates", indexed)
    query = GraphMemParsedQuery(
        raw="What did Alice do at her birthday picnic and when she bought the clock?",
        hierarchy_kind=HierarchyKind.TIME,
        hierarchy_role=HierarchyRole.TIME_SPAN,
        navigation_needs=QueryNeeds(
            mode="multi_step",
            entities=("Alice",),
            facets=("birthday picnic", "bought antique clock"),
        ),
    )
    baseline = GraphNavigationPolicy(
        strategy="relational",
        seed_count=1,
        max_hops=1,
        max_neighbors=2,
        branch_width=2,
        include_original=False,
        ancestor_hops=0,
        initial_candidate_limit=1,
        summary_candidate_limit=3,
    )
    entry = [ScoredMemoryUnit(nodes["seed"], 1)]
    old = navigation.plan_relational(domain, baseline, HOME, query, entry)
    assert {c.unit.id for c in old.candidates} == {"seed", "head", "lower"}
    from dataclasses import replace

    trial = navigation.plan_relational(
        domain, replace(baseline, probe_uncovered_clauses=True), HOME, query, entry
    )
    result = {c.unit.id for c in trial.candidates}
    assert result == ({"seed", "head", expected} if expected else {"seed", "head"})
    assert old.diagnostics["summary_ids_probed"] == trial.diagnostics["summary_ids_probed"] == 3
    assert trial.diagnostics["uncovered_clause_probe_promoted"] == 1


def test_uncovered_clause_requires_event_source_actor_and_time():
    from jiuwen_memory.common.graphmem_semantics import EventTime, SceneEvent

    alice, bob = EntityLabel("Alice", "person"), EntityLabel("Bob", "person")
    good_time = EventTime("2025-06-10T00:00:00Z", "2025-06-11T00:00:00Z", "day", "occurrence")
    old_time = EventTime("2024-06-10T00:00:00Z", "2024-06-11T00:00:00Z", "day", "occurrence")
    needs = QueryNeeds(
        mode="multi_step",
        entities=("Alice",),
        facets=("birthday picnic", "bought antique clock"),
        time_from="2025-06-01T00:00:00Z",
    )
    choices = {}
    for label, entity, time, sources in (
        ("unknown_date", alice, EventTime(), (0,)),
        ("outside", alice, old_time, (0,)),
        ("other_actor", bob, good_time, (0,)),
        ("no_source", alice, good_time, ()),
        ("qualified", alice, good_time, (0,)),
    ):
        choices[label] = GraphSemantics(
            (entity,),
            (SceneEvent("bought antique clock", (entity.key,), "completed", time, sources),),
        ).encode()
    assert navigation._uncovered_probe(choices, choices, {"C2": ["Alice"]}, needs) == "qualified"


def test_ranked_semantic_endpoint_requires_attribution_and_an_existing_semantic_edge():
    from jiuwen_memory.common.graphmem_semantics import EventTime, SceneEvent

    alice, bob = EntityLabel("Alice", "person"), EntityLabel("Bob", "person")
    today = EventTime("2025-06-10", "2025-06-11", "day", "occurrence")
    old = EventTime("2024-06-10", "2024-06-11", "day", "occurrence")
    needs = QueryNeeds(
        mode="multi_step",
        entities=("Alice",),
        facets=("birthday picnic", "bought antique clock"),
        time_from="2025-06-01T00:00:00Z",
    )
    cases = {
        "other_actor": (bob, today, (0,), 1),
        "outside_window": (alice, old, (0,), 1),
        "unlinked": (alice, today, (0,), 1),
        "reference_missing": (alice, today, (), 1),
        "weak_rank": (alice, today, (0,), 4),
        "eligible": (alice, today, (0,), 3),
    }
    events, ranks = {}, {}
    for uid, (entity, interval, sources, rank) in cases.items():
        events[uid] = GraphSemantics(
            (entity,),
            (
                SceneEvent(
                    "collected inherited timepiece", (entity.key,), "completed", interval, sources
                ),
            ),
        ).encode()
        ranks[uid] = {"vector_l2": rank}
    gap = {"C2": ["Alice"]}
    assert navigation._uncovered_probe(events, list(cases), gap, needs) is None
    assert (
        navigation._semantic_endpoint(
            events, ranks, list(cases), gap, needs, set(cases) - {"unlinked"}
        )
        == "eligible"
    )


def test_ranked_semantic_probe_does_not_mark_a_missing_clause_as_grounded(monkeypatch):
    from jiuwen_memory.common.graphmem_semantics import EventTime, SceneEvent

    alice = EntityLabel("Alice", "person")
    scenes = {
        "seed": "birthday picnic",
        "head": "birthday picnic",
        "tail": "birthday picnic",
        "alternate": "collected inherited timepiece",
    }
    nodes = {}
    for uid, scene in scenes.items():
        node = tree(uid, uid, text=f"Alice {scene}.")[0]
        node.system_metadata[FEATURES_KEY] = GraphSemantics(
            (alice,),
            (SceneEvent(scene, (alice.key,), "completed", EventTime(), (0,)),),
        ).encode()
        nodes[uid] = node
    edges = [
        SimpleNamespace(
            id=f"edge-{uid}",
            source="seed",
            target=uid,
            properties={
                "attributes": ["semantic"],
                "score": 0.8,
                "fingerprints": {
                    "seed": memory_fingerprint(nodes["seed"]),
                    uid: memory_fingerprint(nodes[uid]),
                },
            },
        )
        for uid in ("head", "tail", "alternate")
    ]
    domain, _, _, _ = world(list(nodes.values()))
    monkeypatch.setattr(domain, "related_edges", lambda *args, **kwargs: edges)

    def indexed(cache, query, scope, ids, role, limit, needs, *args, **kwargs):
        kwargs["entry_events"].update(
            {uid: nodes[uid].system_metadata[FEATURES_KEY] for uid in ids}
        )
        kwargs["entry_vector_ranks"].update(
            {"head": {"vector_l2": 1}, "alternate": {"vector_l2": 2}}
        )
        return {"head": 0.9, "tail": 0.8, "alternate": 0.7}, {"alternate": 0.8}, "head"

    monkeypatch.setattr(navigation, "_index_candidates", indexed)
    query = GraphMemParsedQuery(
        raw="What did Alice do at her birthday picnic and when she bought the antique clock?",
        hierarchy_kind=HierarchyKind.TIME,
        hierarchy_role=HierarchyRole.TIME_SPAN,
        navigation_needs=QueryNeeds(
            mode="multi_step",
            entities=("Alice",),
            facets=("birthday picnic", "bought antique clock"),
        ),
    )
    policy = GraphNavigationPolicy(
        strategy="relational",
        seed_count=1,
        max_hops=1,
        max_neighbors=2,
        branch_width=2,
        include_original=False,
        ancestor_hops=0,
        initial_candidate_limit=1,
        summary_candidate_limit=3,
        probe_uncovered_clauses=True,
        semantic_endpoint_probe="compound",
    )
    plan = navigation.plan_relational(
        domain, policy, HOME, query, [ScoredMemoryUnit(nodes["seed"], 1)]
    )
    assert {c.unit.id for c in plan.candidates} == {"seed", "head", "alternate"}
    assert plan.diagnostics["summary_ids_probed"] == 3
    assert plan.diagnostics["native_semantic_endpoint_promoted"]
    reason = next(e["reason"] for e in plan.links if e["target"][-1] == "alternate")
    assert reason["semantic_probe"] and reason["ranked_vector_endpoint"]
    assert "scene_clauses" not in reason and reason["clause_transition"]["to"] == []
