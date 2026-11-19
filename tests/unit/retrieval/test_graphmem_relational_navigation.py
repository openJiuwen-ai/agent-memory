"""Relation programs constrain reads and return an attributable, budgeted text graph."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.graphmem_semantics import (
    FEATURES_KEY,
    EntityLabel,
    EventTime,
    GraphSemantics,
    SceneEvent,
)
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRole,
    RecallBatch,
    RecallChannel,
    RecallResult,
    ScoredMemoryUnit,
    ScoredUnit,
)
from jiuwen_memory.construction.associator_impl.graphmem_topdown import (
    TopDownGraphMemAssociator,
    TopDownRelationPolicy,
)
from jiuwen_memory.retrieval.evidence_packing import EvidencePacker
from jiuwen_memory.retrieval.graph_navigation import GraphEvidenceNavigator, GraphNavigationPolicy
from jiuwen_memory.retrieval.graphmem_query import (
    GraphMemParsedQuery,
    QueryNeeds,
    entry_keyword_query,
)
from jiuwen_memory.retrieval.graphmem_relation_plan import (
    candidate_limits,
    child_event_hints,
    compile_program,
    detail_vector_leader,
    ground_query_entities,
    limit_recalled,
    node_signals,
)
from jiuwen_memory.retrieval.graphmem_relation_selection import edge_hint, relation_reason
from jiuwen_memory.retrieval.graphmem_relational_navigation import (
    RelationalEvidencePlan,
    _index_candidates,
    plan_relational,
)
from tests.unit.construction.graphmem_fixtures import HOME, Words, sync, tree, world

pytestmark = pytest.mark.unit


def bind_fixture_index(domain, units):
    """Deterministic ID-only replies for graph tests; real get still checks sources."""

    def recall(scope, query, *, channels, recall_limit):
        eligible = [
            u for u in units if u.scope == scope and u.hierarchy.role is query.hierarchy_role
        ]
        return RecallResult(
            batches=[
                RecallBatch(
                    RecallChannel.KEYWORD,
                    "l2",
                    [ScoredUnit(u.id, 1.0) for u in eligible[:recall_limit]],
                )
            ]
            if RecallChannel.KEYWORD in channels
            else []
        )

    domain.recall = recall


def fixture():
    pairs = [
        tree("seed", "s1", text="Alice riverside picnic", raw="Alice brought tea."),
        tree("related", "s2", text="Alice riverside picnic lunch", raw="Alice brought bread."),
        tree("other", "s3", text="Alice mountain climbing", raw="Alice climbed a mountain."),
    ]
    parents, leaves = map(list, zip(*pairs))
    for parent in parents:
        parent.system_metadata[FEATURES_KEY] = GraphSemantics(
            (EntityLabel("Alice", "person"),), ()
        ).encode()
    domain, _, _, writer = world([*parents, *leaves])
    bind_fixture_index(domain, [*parents, *leaves])
    sync(TopDownGraphMemAssociator(Words()), writer, parents)
    query = GraphMemParsedQuery(
        raw="What did Alice bring to the riverside picnic?",
        hierarchy_kind=HierarchyKind.TIME,
        hierarchy_role=HierarchyRole.TIME_SPAN,
        navigation_needs=QueryNeeds(
            "fact", ("Alice",), ("riverside picnic",), analysis_status="used", model_calls=1
        ),
    )
    policy = GraphNavigationPolicy(
        strategy="relational",
        seed_count=1,
        max_neighbors=2,
        max_reads=32,
        source_limit=2,
        initial_candidate_limit=4,
        summary_candidate_limit=8,
        source_candidate_limit=4,
        branch_width=2,
        summary_token_budget=1000,
    )
    packer = EvidencePacker(len, "characters", mode="sources_first", header_style="compact")
    return domain, parents, leaves, query, policy, packer


def test_entry_keyword_query_adds_analyzed_topics_without_replacing_query_vector():
    query = GraphMemParsedQuery(
        raw="Which trips happened before July?",
        vector=[0.1, 0.2],
        navigation_needs=QueryNeeds(facets=("road trip", "road trips"), analysis_status="used"),
    )
    entry = entry_keyword_query(query)
    assert entry.rewritten == "Which trips happened before July? road trip"
    assert entry.raw == query.raw
    assert entry.vector is query.vector
    assert entry.scalar_filters is query.scalar_filters
    assert entry.navigation_needs is query.navigation_needs
    assert entry_keyword_query(entry) is entry


def test_query_program_compiles_collection_and_time_order_without_new_inference():
    query = GraphMemParsedQuery(
        raw="Which trips happened before July?",
        navigation_needs=QueryNeeds(
            "collection", (), ("travel", "visit"), "2023-01-01", "2023-07-01"
        ),
    )
    program = compile_program(query)
    assert program.combine == "union" and "temporal" in program.relations
    assert [o["op"] for o in program.describe()["operations"]] == [
        "select",
        "expand",
        "union",
        "order",
    ]


def test_initial_id_cap_avoids_counting_multiple_layers_as_independent_votes():
    result = RecallResult(
        batches=[
            RecallBatch(RecallChannel.KEYWORD, "l0", [ScoredUnit("a"), ScoredUnit("b")]),
            RecallBatch(RecallChannel.KEYWORD, "l1", [ScoredUnit("a"), ScoredUnit("b")]),
            RecallBatch(RecallChannel.VECTOR, "l2", [ScoredUnit("b"), ScoredUnit("c")]),
        ]
    )
    limited = limit_recalled(result, 1)
    assert {c.unit_id for b in limited.batches for c in b.candidates} == {"b"}


def test_single_entry_tie_preserves_original_query_vector_independent_of_batch_order():
    batches = [
        RecallBatch(RecallChannel.KEYWORD, "l2", [ScoredUnit("lexical")]),
        RecallBatch(RecallChannel.VECTOR, "l2", [ScoredUnit("semantic")]),
    ]
    for order in (batches, list(reversed(batches))):
        result = limit_recalled(RecallResult(batches=order), 1)
        assert {c.unit_id for b in result.batches for c in b.candidates} == {"semantic"}


@pytest.mark.parametrize("limit", [1, 2])
def test_detailed_vector_head_survives_coarse_agreement_within_the_same_id_limit(limit):
    batches = [
        RecallBatch(
            RecallChannel.VECTOR, "vector_l2", [ScoredUnit("detail", 0.9), ScoredUnit("broad", 0.8)]
        ),
        RecallBatch(RecallChannel.VECTOR, "vector_l0", [ScoredUnit("broad", 0.9)]),
        RecallBatch(RecallChannel.KEYWORD, "keyword_l1", [ScoredUnit("broad", 0.9)]),
    ]
    for order in (batches, list(reversed(batches))):
        result = limit_recalled(RecallResult(batches=order), limit)
        kept = {c.unit_id for b in result.batches for c in b.candidates}
        assert "detail" in kept and len(kept) == limit
    assert len(batches[0].candidates) == 2, "The native result must not be mutated"


def test_detail_vector_head_uses_only_finite_hits_in_the_allowed_pool():
    batches = [
        RecallBatch(
            RecallChannel.VECTOR,
            "vector_l2",
            [
                ScoredUnit("invalid", float("nan")),
                ScoredUnit("outside", 0.9),
                ScoredUnit("inside", 0.8),
            ],
        )
    ]
    assert detail_vector_leader(batches, ["invalid", "inside"]) == "inside"
    assert detail_vector_leader(batches, []) is None
    assert not any(b.candidates for b in limit_recalled(RecallResult(batches=batches), 0).batches)


def test_keyword_head_can_keep_second_entry_within_same_limit():
    recalled = RecallResult(
        batches=[
            RecallBatch(RecallChannel.VECTOR, "vector_l2", [ScoredUnit("detail", 0.9)]),
            RecallBatch(RecallChannel.VECTOR, "vector_l1", [ScoredUnit("overview", 0.9)]),
            RecallBatch(RecallChannel.KEYWORD, "keyword_l1", [ScoredUnit("overview", 0.9)]),
            RecallBatch(RecallChannel.KEYWORD, "keyword_l2", [ScoredUnit("exact", 4.0)]),
        ]
    )
    for limit, expected in ((1, {"detail"}), (2, {"detail", "exact"})):
        limited = limit_recalled(recalled, limit, detail_keyword=True)
        assert {c.unit_id for batch in limited.batches for c in batch.candidates} == expected


def test_relation_selects_relevant_entity_scene_and_returns_verified_graph():
    domain, parents, _, query, policy, packer = fixture()
    selected, bundle = GraphEvidenceNavigator(domain, policy).pack_closure(
        HOME, query, [ScoredMemoryUnit(parents[0], 1)], packer, max_tokens=12000, top_k=16
    )
    assert {c.unit.id for c in selected} >= {"seed", "related"}
    assert "mountain" not in bundle.rendered_context
    graph = bundle.navigation["evidence_graph"]
    relation = next(e for e in graph["edges"] if e["kind"] == "related")
    assert relation["reason"]["entities"] == ["Alice"]
    assert "[Query plan]" in bundle.rendered_context
    assert "[Evidence graph]" in bundle.rendered_context
    assert bundle.token_count == len(bundle.rendered_context) <= bundle.token_budget
    assert bundle.navigation["summary_ids_probed"] <= policy.summary_candidate_limit
    assert bundle.navigation["source_candidates"] <= policy.source_candidate_limit
    refs = {n["ref"] for n in graph["nodes"]}
    assert all(e["source"] in refs and e["target"] in refs for e in graph["edges"])
    assert len(bundle.sources) == len(selected)
    assert len(selected) == sum(n["kind"] != "navigation_only" for n in graph["nodes"])


def test_graph_overhead_and_analysis_reserve_share_the_context_budget():
    domain, parents, _, query, policy, packer = fixture()
    policy = replace(policy, analysis_token_reserve=128)
    _, bundle = GraphEvidenceNavigator(domain, policy).pack_closure(
        HOME, query, [ScoredMemoryUnit(parents[0], 1)], packer, max_tokens=1700, top_k=16
    )
    assert bundle.token_budget == 1572
    assert bundle.token_count == len(bundle.rendered_context) <= 1572
    graph = bundle.navigation["evidence_graph"]
    refs = {n["ref"] for n in graph.get("nodes", [])}
    assert all(e["source"] in refs and e["target"] in refs for e in graph.get("edges", []))


def test_summary_only_does_not_read_original_sessions(monkeypatch):
    domain, parents, _, query, policy, packer = fixture()
    original_get = domain.get

    def guarded_get(scope, ids, **kwargs):
        assert not scope.session, "summary-only navigation read an original session"
        return original_get(scope, ids, **kwargs)

    monkeypatch.setattr(domain, "get", guarded_get)
    navigator = GraphEvidenceNavigator(domain, replace(policy, include_original=False))
    selected, bundle = navigator.pack_closure(
        HOME, query, [ScoredMemoryUnit(parents[0], 1)], packer, max_tokens=12000, top_k=16
    )
    assert selected and all(c.unit.hierarchy.role is not HierarchyRole.SNAPSHOT for c in selected)
    assert bundle.navigation["source_candidates"] == 0


def test_graph_connectors_survive_omitted_summary_payloads():
    domain, parents, _, query, policy, packer = fixture()
    selected, bundle = GraphEvidenceNavigator(domain, policy).pack_closure(
        HOME,
        query,
        [ScoredMemoryUnit(parents[0], 1)],
        replace(packer, max_summaries=0),
        max_tokens=12000,
        top_k=16,
    )
    assert selected and all(c.unit.hierarchy.role is HierarchyRole.SNAPSHOT for c in selected)
    graph = bundle.navigation["evidence_graph"]
    assert any(e["kind"] == "related" for e in graph["edges"])
    assert any(n["kind"] == "navigation_only" for n in graph["nodes"])
    assert graph["navigation_sources"]
    assert bundle.token_count == len(bundle.rendered_context)


def test_stale_related_endpoint_cannot_supply_evidence():
    domain, parents, _, query, policy, packer = fixture()
    changed = domain.get(HOME, ["related"])[0]
    changed.segments[0].content = "Alice riverside picnic changed after indexing"
    domain.update(HOME, [changed])
    _, bundle = GraphEvidenceNavigator(domain, policy).pack_closure(
        HOME, query, [ScoredMemoryUnit(parents[0], 1)], packer, max_tokens=12000, top_k=16
    )
    assert bundle.navigation["stale_edges"] >= 1
    assert "bread" not in bundle.rendered_context


def test_source_limit_is_shared_across_branches_before_body_reads():
    domain, parents, leaves, query, policy, _ = fixture()
    parent = deepcopy(parents[0])
    extra = [deepcopy(leaves[0]) for _ in range(4)]
    for i, leaf in enumerate(extra):
        leaf.id = f"extra-{i}"
        parent.hierarchy.child_ids.append(leaf.id)
        parent.hierarchy.child_scopes.append(deepcopy(leaf.scope))
    domain.add(leaves[0].scope, extra)
    domain.update(HOME, [parent])
    policy = replace(
        policy, seed_count=2, source_limit=6, source_candidate_limit=2, use_relations=False
    )
    domain.reads.clear()
    plan = plan_relational(
        domain,
        policy,
        HOME,
        query,
        [ScoredMemoryUnit(parent, 1), ScoredMemoryUnit(parents[1], 0.9)],
    )
    sources = [c for c in plan.candidates if c.unit.hierarchy.role is HierarchyRole.SNAPSHOT]
    assert {c.unit.scope.session for c in sources} == {"s1", "s2"}
    assert sum(len(ids) for scope, ids in domain.reads if scope.session) == 2


def test_temporal_reason_requires_matched_event_intervals_not_message_dates():
    _, parents, _, query, _, _ = fixture()
    query.navigation_needs = replace(query.navigation_needs, mode="temporal")
    edge = SimpleNamespace(properties={"attributes": ["temporal"]})
    program = compile_program(query)
    left, right = deepcopy(parents[0]), deepcopy(parents[1])
    assert left.temporal.t_message is not None
    assert not relation_reason(left, right, edge, query.navigation_needs, program)
    entity = EntityLabel("Alice", "person")
    windows = [
        EventTime("2023-06-01", "2023-06-02", "day", "occurrence"),
        EventTime("2023-06-03", "2023-06-04", "day", "occurrence"),
    ]
    events = [SceneEvent("riverside picnic", (entity.key,), "completed", w, (1,)) for w in windows]
    for node, event in zip((left, right), events):
        node.system_metadata[FEATURES_KEY] = GraphSemantics((entity,), (event,)).encode()
    edge.properties["matched_events"] = [{"left": events[0].key, "right": events[1].key}]
    reason = relation_reason(left, right, edge, query.navigation_needs, program)
    assert reason["event_time"]["gap_seconds"] == 86400
    assert reason["event_time"]["from_interval"] == ["2023-06-01", "2023-06-02"]


def test_rule_query_grounds_stored_names_and_aliases_without_substring_matches():
    _, parents, _, query, _, _ = fixture()
    parents[0].system_metadata[FEATURES_KEY] = GraphSemantics(
        (EntityLabel("Alice", "person", ("Dr A",)), EntityLabel("Ann", "person")), ()
    ).encode()
    query.raw = "What did Dr A bring to the anniversary picnic?"
    query.navigation_needs = QueryNeeds(facets=(query.raw,))
    needs = ground_query_entities(query, [ScoredMemoryUnit(parents[0], 1)])
    assert needs.entities == ("Alice",)
    assert needs.model_calls == 0
    assert not query.navigation_needs.entities, "Grounding must not mutate the caller query"


def test_personal_query_cannot_resolve_identity_from_stored_pronoun_alias():
    _, parents, _, query, _, _ = fixture()
    parents[0].system_metadata[FEATURES_KEY] = GraphSemantics(
        (EntityLabel("Alice", "person", ("I",)), EntityLabel("user", "person")), ()
    ).encode()
    query.raw = "Which countries did I visit?"
    query.navigation_needs = QueryNeeds(facets=("countries visited",))
    assert not ground_query_entities(query, [ScoredMemoryUnit(parents[0], 1)]).entities


def test_ratio_limits_precede_reads_and_never_expand_small_bank_to_absolute_cap():
    domain, _, _, query, policy, _ = fixture()
    counts = {HierarchyRole.SNAPSHOT: 153, HierarchyRole.TIME_SPAN: 29}
    domain.indexed_population = lambda scope, parsed: counts[parsed.hierarchy_role]
    policy = replace(
        policy,
        initial_candidate_limit=8,
        summary_candidate_limit=32,
        source_candidate_limit=128,
        source_candidate_ratio=0.1,
        summary_candidate_ratio=0.25,
    )
    limits = candidate_limits(domain, policy, HOME, query)
    assert limits["source_limit"] == 15
    assert limits["summary_limit"] == 7
    assert limits["initial_limit"] == 2, "Reserve summary budget for relation traversal"
    assert not domain.reads
    balanced = candidate_limits(domain, replace(policy, initial_summary_fraction=0.5), HOME, query)
    assert balanced["initial_limit"] == 3
    assert balanced["summary_limit"] == limits["summary_limit"]
    assert balanced["source_limit"] == limits["source_limit"]
    counts[HierarchyRole.SNAPSHOT] = 0
    assert candidate_limits(domain, policy, HOME, query)["source_limit"] == 0


@pytest.mark.parametrize("value", [True, False, 0, -0.1, 1.01, float("nan"), float("inf")])
def test_initial_summary_share_rejects_invalid_values(value):
    from jiuwen_memory.common.errors import ValidationError

    with pytest.raises(ValidationError):
        GraphNavigationPolicy(strategy="relational", initial_summary_fraction=value)


def test_unsupported_ratio_count_does_not_fall_back_to_full_memory_scan(monkeypatch):
    from jiuwen_memory.common.errors import UnsupportedCapabilityError

    domain, _, _, query, policy, _ = fixture()
    policy = replace(policy, source_candidate_ratio=0.1)

    def forbidden(*args, **kwargs):
        raise AssertionError("Population must not scan/load all memories")

    monkeypatch.setattr(domain, "list", forbidden)
    monkeypatch.setattr(domain, "get", forbidden)
    with pytest.raises(UnsupportedCapabilityError):
        candidate_limits(domain, policy, HOME, query)


def test_positive_cosine_does_not_prove_same_entity_has_query_scene():
    _, parents, _, query, _, _ = fixture()
    alice = EntityLabel("Alice", "person")
    edge = SimpleNamespace(
        properties={"attributes": ["entity", "semantic"], "shared_entities": [alice.key]}
    )
    assert not relation_reason(
        parents[0],
        parents[2],
        edge,
        query.navigation_needs,
        compile_program(query),
        query_similarity=0.7,
    )
    probe = relation_reason(
        parents[0],
        parents[2],
        edge,
        query.navigation_needs,
        compile_program(query),
        query_similarity=0.7,
        allow_semantic_probe=True,
    )
    assert probe["semantic_probe"] and "scene_clauses" not in probe
    assert "entities" not in probe, "An exploratory similarity is not an entity/scene proof"


def test_clauses_bind_names_to_each_event_not_the_entire_summary():
    _, parents, _, _, _, _ = fixture()
    alice, bob = EntityLabel("Alice", "person"), EntityLabel("Bob", "person")
    parents[0].segments[0].content = "Bob went to a picnic. Alice was climbing."
    parents[0].system_metadata[FEATURES_KEY] = GraphSemantics(
        (alice, bob),
        (
            SceneEvent("picnic", (bob.key,), "completed", EventTime(), (1,)),
            SceneEvent("climbing", (alice.key,), "completed", EventTime(), (2,)),
        ),
    ).encode()
    needs = QueryNeeds("comparison", ("Alice", "Bob"), ("Alice picnic", "Bob climbing"))
    assert not node_signals(parents[0], needs)["clauses"]
    needs = replace(needs, facets=("Bob picnic", "Alice climbing"))
    assert node_signals(parents[0], needs)["clauses"] == ["C1", "C2"]


def test_detailed_summary_sentence_recovers_coarse_scene_without_cross_sentence_join():
    _, parents, _, query, _, _ = fixture()
    alice, bob = EntityLabel("Alice", "person"), EntityLabel("Bob", "person")
    parents[0].system_metadata[FEATURES_KEY] = GraphSemantics(
        (alice, bob),
        (SceneEvent("outdoor activities", (alice.key, bob.key), "completed", EventTime(), (1,)),),
    ).encode()
    parents[0].layers.l1 = "outdoor activities"
    parents[0].segments[0].content = "Alice brought tea to the riverside picnic. Bob went climbing."
    signals = node_signals(parents[0], query.navigation_needs)
    assert signals["clauses"] == ["C1"] and signals["text_bindings"]
    parents[0].segments[0].content = "Alice went climbing. Bob brought tea to the riverside picnic."
    assert not node_signals(parents[0], query.navigation_needs)["clauses"]


def test_relation_branches_recompute_marginal_coverage_after_each_admission():
    _, _, _, query, policy, _ = fixture()
    query.navigation_needs = QueryNeeds(
        "multi_step", ("Alice",), ("picnic", "climbing", "painting")
    )
    pairs = [
        tree(uid, uid, text=f"Alice {scene}")
        for uid, scene in (
            ("seed", "picnic"),
            ("climb-1", "climbing"),
            ("climb-2", "climbing"),
            ("paint", "painting"),
        )
    ]
    parents, leaves = map(list, zip(*pairs))
    for parent in parents:
        parent.system_metadata[FEATURES_KEY] = GraphSemantics(
            (EntityLabel("Alice", "person"),), ()
        ).encode()
    domain, _, _, writer = world([*parents, *leaves])
    bind_fixture_index(domain, [*parents, *leaves])
    associator = TopDownGraphMemAssociator(
        Words(), policy=TopDownRelationPolicy(common_entity_sessions=10)
    )
    sync(associator, writer, parents)
    plan = plan_relational(
        domain,
        replace(policy, max_hops=1, max_neighbors=2),
        HOME,
        query,
        [ScoredMemoryUnit(parents[0], 1)],
    )
    chosen = {
        c.unit.id for c in plan.candidates if c.unit.hierarchy.role is HierarchyRole.TIME_SPAN
    }
    assert "paint" in chosen
    assert len(chosen & {"climb-1", "climb-2"}) == 1


def test_irrelevant_entity_only_edge_is_rejected_before_endpoint_read():
    _, parents, _, query, _, _ = fixture()
    alice, bob = EntityLabel("Alice", "person"), EntityLabel("Bob", "person")
    parents[0].system_metadata[FEATURES_KEY] = GraphSemantics((alice, bob), ()).encode()
    edge = SimpleNamespace(properties={"attributes": ["entity"], "shared_entities": [bob.key]})
    assert edge_hint(parents[0], edge, query.navigation_needs, compile_program(query)) is None


def test_explicit_calendar_window_enables_temporal_selection_even_in_fact_mode():
    query = GraphMemParsedQuery(
        raw="Alice picnic in June 2023",
        navigation_needs=QueryNeeds("fact", ("Alice",), ("picnic",), "2023-06-01", "2023-07-01"),
    )
    assert "temporal" in compile_program(query).relations


def test_temporal_choice_matches_query_event_before_minimizing_gap():
    _, parents, _, query, _, _ = fixture()
    alice = EntityLabel("Alice", "person")
    query.navigation_needs = replace(query.navigation_needs, mode="temporal")
    left_events, right_events = [], []
    for events, day in ((left_events, 1), (right_events, 3)):
        for scene, number in (("riverside picnic", day), ("guitar lesson", 10)):
            window = EventTime(
                f"2023-06-{number:02}", f"2023-06-{number + 1:02}", "day", "occurrence"
            )
            events.append(SceneEvent(scene, (alice.key,), "completed", window, (1,)))
    for node, events in zip(parents, (left_events, right_events)):
        node.system_metadata[FEATURES_KEY] = GraphSemantics((alice,), tuple(events)).encode()
    edge = SimpleNamespace(
        properties={
            "attributes": ["temporal"],
            "matched_events": [
                {"left": a.key, "right": b.key} for a, b in zip(left_events, right_events)
            ],
        }
    )
    reason = relation_reason(
        parents[0], parents[1], edge, query.navigation_needs, compile_program(query)
    )
    assert reason["event_time"]["gap_seconds"] == 86400
    assert reason["event_time"]["from_event"] == left_events[0].key


def test_before_after_is_relative_to_anchor_independent_of_traversal_direction():
    _, parents, _, query, _, _ = fixture()
    alice = EntityLabel("Alice", "person")
    query.raw = "What did Alice do after the riverside picnic?"
    query.navigation_needs = QueryNeeds("temporal", ("Alice",), ("riverside picnic", "cycling"))
    events = [
        SceneEvent(
            scene,
            (alice.key,),
            "completed",
            EventTime(f"2023-06-{day:02}", f"2023-06-{day + 1:02}", "day", "occurrence"),
            (1,),
        )
        for scene, day in (("riverside picnic", 1), ("cycling", 3))
    ]
    for parent, event in zip(parents, events):
        parent.system_metadata[FEATURES_KEY] = GraphSemantics((alice,), (event,)).encode()
    edge = SimpleNamespace(
        properties={
            "attributes": ["temporal"],
            "matched_events": [{"left": events[0].key, "right": events[1].key}],
        }
    )
    for left, right in ((parents[0], parents[1]), (parents[1], parents[0])):
        reason = relation_reason(left, right, edge, query.navigation_needs, compile_program(query))
        assert reason["event_time"]["condition"] == "after"
        assert reason["event_time"]["anchor_event"] == events[0].key
    query.raw = "What did Alice do before the riverside picnic?"
    assert not relation_reason(
        parents[0], parents[1], edge, query.navigation_needs, compile_program(query)
    )


def test_known_outside_window_cannot_be_relabelled_as_semantic_probe():
    _, parents, _, query, _, _ = fixture()
    alice = EntityLabel("Alice", "person")
    event = SceneEvent(
        "riverside picnic",
        (alice.key,),
        "completed",
        EventTime("2022-06-01", "2022-06-02", "day", "occurrence"),
        (1,),
    )
    parents[1].system_metadata[FEATURES_KEY] = GraphSemantics((alice,), (event,)).encode()
    query.navigation_needs = replace(
        query.navigation_needs, time_from="2023-06-01", time_to="2023-07-01"
    )
    edge = SimpleNamespace(
        properties={"attributes": ["semantic", "entity"], "shared_entities": [alice.key]}
    )
    assert not relation_reason(
        parents[0],
        parents[1],
        edge,
        query.navigation_needs,
        compile_program(query),
        0.8,
        allow_semantic_probe=True,
    )


def test_relation_only_connectors_survive_without_claiming_evidence_coverage():
    from jiuwen_memory.retrieval.expander import node_key
    from jiuwen_memory.retrieval.graphmem_evidence_graph import render_graph

    _, parents, leaves, query, _, _ = fixture()
    items = [ScoredMemoryUnit(u, 1) for u in [*parents, leaves[2]]]
    keys = [node_key(c.unit) for c in items]
    plan = RelationalEvidencePlan(
        candidates=items,
        program=compile_program(query).describe(),
        reasons={
            k: {
                "clauses": ["C1"] if i < 3 else [],
                "selected_by": "initial_vector_keyword_match"
                if i == 0
                else "query_relevant_relation",
            }
            for i, k in enumerate(keys)
        },
        links=[
            {
                "source": keys[i],
                "target": keys[i + 1],
                "kind": "related" if i < 2 else "contains",
                "reason": {},
            }
            for i in range(3)
        ],
    )
    context, graph = render_graph([items[-1]], plan)
    assert len(graph["navigation_sources"]) == 3
    assert len(graph["paths"][0]["steps"]) == 3
    assert graph["clause_bindings"] == {"C1": []}
    assert graph["unmatched_clauses"] == ["C1"]
    assert len(graph["navigation_clause_bindings"]["C1"]) == 3
    assert "navigation_only" in context and "Alice riverside picnic" not in context


@pytest.mark.parametrize("include_summaries", [False, True])
def test_graph_order_finishes_native_source_sequence_before_related_conversation(include_summaries):
    from jiuwen_memory.retrieval.expander import node_key
    from jiuwen_memory.retrieval.graphmem_evidence_graph import graph_order

    _, parents, leaves, query, _, _ = fixture()
    later = deepcopy(leaves[0])
    later.id = "turn-2"
    parents[0].hierarchy.child_ids.append(later.id)
    parents[0].hierarchy.child_scopes.append(deepcopy(later.scope))
    # Retrieval may rank the later reply first; the public text graph should
    # retain the native conversation sequence and finish it before crossing edges.
    units = [parents[0], parents[1], later, leaves[1], leaves[0]]
    items = [ScoredMemoryUnit(u, 1) for u in units]
    a, b, late, neighbor, early = [node_key(u) for u in units]
    plan = RelationalEvidencePlan(
        candidates=items,
        program=compile_program(query).describe(),
        links=[
            {"source": a, "target": b, "kind": "related"},
            {"source": b, "target": a, "kind": "related"},
            {"source": a, "target": late, "kind": "contains"},
            {"source": b, "target": neighbor, "kind": "contains"},
            {"source": a, "target": early, "kind": "contains"},
        ],
    )
    selected = items if include_summaries else items[2:]
    ordered = graph_order(selected, plan)
    expected = [a, early, late, b, neighbor] if include_summaries else [early, late, neighbor]
    assert [node_key(c.unit) for c in ordered] == expected
    assert [node_key(c.unit) for c in plan.candidates] == [a, b, late, neighbor, early]
    assert len(ordered) == len(selected), "Navigation duplicated a cycle or exposed hidden text"


def test_empty_index_does_not_materialize_prefix_sources_or_unranked_relation_targets(monkeypatch):
    domain, parents, _, query, policy, _ = fixture()
    monkeypatch.setattr(domain, "recall", lambda *a, **kw: RecallResult())
    domain.reads.clear()
    plan = plan_relational(domain, policy, HOME, query, [ScoredMemoryUnit(parents[0], 1)])
    assert not domain.reads, "Empty index results caused arbitrary endpoint/source reads"
    assert [c.unit.id for c in plan.candidates] == [parents[0].id]
    assert plan.diagnostics["source_index_ids_returned"] == 0
    assert plan.diagnostics["source_candidates"] == 0
    assert plan.diagnostics["empty_index_branches"] == 1
    assert plan.diagnostics["index_fallbacks"] == 0


def test_odd_source_quota_is_shared_exactly_without_unranked_fill():
    _, _, _, query, _, _ = fixture()
    query.vector = [0.5, 0.5]
    calls = []

    def recall(scope, parsed, *, channels, recall_limit):
        calls.append((channels, recall_limit))
        ids = ["a", "b"] if channels == [RecallChannel.VECTOR] else ["a"]
        return RecallResult(
            batches=[
                RecallBatch(channels[0], "l2", [ScoredUnit(uid, 0.8) for uid in ids[:recall_limit]])
            ]
        )

    scores, vectors, detail_leader = _index_candidates(
        SimpleNamespace(recall=recall),
        query,
        HOME,
        ["a", "b", "unranked"],
        HierarchyRole.SNAPSHOT,
        3,
        query.navigation_needs,
        strict_pool=True,
    )
    assert calls == [([RecallChannel.VECTOR], 2), ([RecallChannel.KEYWORD], 1)]
    assert set(scores) == set(vectors) == {"a", "b"}
    assert detail_leader is None, "Raw-source allocation keeps its existing channel quotas"
    assert scores["a"] > scores["b"], "Channel agreement should still influence admission"


def test_partial_index_hits_do_not_fill_remaining_slots_with_prefix_children(monkeypatch):
    domain, parents, leaves, query, policy, _ = fixture()
    parent = deepcopy(parents[0])
    children = [deepcopy(leaves[0]) for _ in range(3)]
    for i, child in enumerate(children):
        child.id = f"part-{i}"
    parent.hierarchy.child_ids = [u.id for u in children]
    parent.hierarchy.child_scopes = [deepcopy(u.scope) for u in children]
    domain.add(children[0].scope, children)
    domain.update(HOME, [parent])
    monkeypatch.setattr(
        domain,
        "recall",
        lambda *a, **kw: RecallResult(
            batches=[RecallBatch(RecallChannel.KEYWORD, "l2", [ScoredUnit("part-1", 0.8)])]
        ),
    )
    domain.reads.clear()
    policy = replace(
        policy, use_relations=False, context_radius=0, source_limit=3, source_candidate_limit=3
    )
    plan = plan_relational(domain, policy, HOME, query, [ScoredMemoryUnit(parent, 1)])
    assert [c.unit.id for c in plan.candidates] == [parent.id, "part-1"]
    assert [ids for scope, ids in domain.reads if scope.session] == [["part-1"]]
    assert plan.diagnostics["source_candidates"] == 1


def event_reference_fixture():
    from jiuwen_memory.common.graphmem_structure import RECURSIVE_VERSION

    domain, parents, leaves, query, policy, packer = fixture()
    parent = deepcopy(parents[0])
    children = [deepcopy(leaves[0]) for _ in range(4)]
    for i, child in enumerate(children):
        child.id = f"event-part-{i}"
    parent.hierarchy.child_ids = [u.id for u in children]
    parent.hierarchy.child_scopes = [deepcopy(u.scope) for u in children]
    session = children[0].scope
    parent.system_metadata.update(
        graphmem_recursive_version=RECURSIVE_VERSION,
        graphmem_height="1",
        graphmem_child_roles=[HierarchyRole.SNAPSHOT.value] * 4,
        graphmem_session_scope=json.dumps(
            (session.org, session.space, session.user, session.agent, session.session)
        ),
    )
    alice = EntityLabel("Alice", "person")
    event = SceneEvent("riverside picnic", (alice.key,), "completed", EventTime(), (2,))
    parent.system_metadata[FEATURES_KEY] = GraphSemantics((alice,), (event,)).encode()
    domain.add(children[0].scope, children)
    domain.update(HOME, [parent])
    policy = replace(
        policy, use_relations=False, context_radius=0, source_limit=2, source_candidate_limit=2
    )
    return domain, parent, children, query, policy, packer, event


def test_matching_event_source_reference_guides_index_before_reading_under_same_quota(monkeypatch):
    from jiuwen_memory.common.type_def import FilterClause

    domain, parent, children, query, policy, _, event = event_reference_fixture()
    calls = []

    def allowed_ids(filters):
        if isinstance(filters, FilterClause):
            return filters.value if filters.field == "unit_id" else []
        return [uid for child in filters.children for uid in allowed_ids(child)]

    def recall(scope, parsed, *, channels, recall_limit):
        ids = allowed_ids(parsed.scalar_filters)
        calls.append((scope, ids, recall_limit))
        # The ordinary rank prefers an unrelated first source. A scoped hint
        # can reach the event's actual source without admitting more candidates.
        selected = sorted(ids)[:recall_limit]
        return RecallResult(
            batches=[RecallBatch(channels[0], "l2", [ScoredUnit(uid, 0.8) for uid in selected])]
        )

    monkeypatch.setattr(domain, "recall", recall)
    domain.reads.clear()
    plan = plan_relational(domain, policy, HOME, query, [ScoredMemoryUnit(parent, 1)])
    sources = [c for c in plan.candidates if c.unit.hierarchy.role is HierarchyRole.SNAPSHOT]
    assert {c.unit.id for c in sources} == {children[0].id, children[2].id}
    assert calls[0][1] == [children[2].id] and children[2].id not in calls[1][1]
    assert sum(len(ids) for scope, ids in domain.reads if scope.session) == 2
    assert plan.diagnostics["source_index_ids_returned"] == 2
    assert plan.diagnostics["event_source_candidates"] == 1
    link = next(e for e in plan.links if e["target"][-1] == children[2].id)
    assert link["reason"]["event_references"] == [
        {"event_id": event.key, "source_position": 2, "clauses": ["C1"]}
    ]


@pytest.mark.parametrize("position", [-1, 4, True, "2"])
def test_event_source_hint_rejects_invalid_or_untyped_child_ordinal(position):
    _, parent, children, query, _, _, event = event_reference_fixture()
    parent.system_metadata[FEATURES_KEY] = GraphSemantics(
        (EntityLabel("Alice", "person"),), (replace(event, sources=(position,)),)
    ).encode()
    assert not child_event_hints(
        parent,
        query.navigation_needs,
        children[0].scope,
        parent.hierarchy.child_ids,
        HierarchyRole.SNAPSHOT,
    )


def test_event_source_hint_cannot_cross_scope_role_or_query_conditions():
    _, parent, children, query, _, _, event = event_reference_fixture()
    args = parent, query.navigation_needs, children[0].scope, parent.hierarchy.child_ids
    assert child_event_hints(*args, HierarchyRole.SNAPSHOT)
    assert not child_event_hints(*args, HierarchyRole.TIME_SPAN)
    assert not child_event_hints(
        parent, query.navigation_needs, HOME, parent.hierarchy.child_ids, HierarchyRole.SNAPSHOT
    )
    parent.system_metadata[FEATURES_KEY] = GraphSemantics(
        (EntityLabel("Alice", "person"),), (replace(event, scene="mountain climbing"),)
    ).encode()
    assert not child_event_hints(*args, HierarchyRole.SNAPSHOT)


@pytest.mark.parametrize("condition", ["wrong_entity", "outside_time"])
def test_event_source_hint_rejects_mismatched_event_conditions(condition):
    _, parent, children, query, _, _, event = event_reference_fixture()
    alice, bob = EntityLabel("Alice", "person"), EntityLabel("Bob", "person")
    needs = query.navigation_needs
    if condition == "wrong_entity":
        event = replace(event, entity_keys=(bob.key,))
    else:
        event = replace(
            event,
            time=EventTime(
                "2023-01-01T00:00:00+00:00", "2023-01-02T00:00:00+00:00", "day", "occurrence"
            ),
        )
        needs = replace(needs, time_from="2023-06-01T00:00:00+00:00")
    parent.system_metadata[FEATURES_KEY] = GraphSemantics((alice, bob), (event,)).encode()
    assert not child_event_hints(
        parent, needs, children[0].scope, parent.hierarchy.child_ids, HierarchyRole.SNAPSHOT
    )


def test_event_hint_without_index_hit_does_not_read_source(monkeypatch):
    domain, parent, _, query, policy, _, _ = event_reference_fixture()
    monkeypatch.setattr(domain, "recall", lambda *a, **kw: RecallResult())
    domain.reads.clear()
    plan = plan_relational(domain, policy, HOME, query, [ScoredMemoryUnit(parent, 1)])
    assert [c.unit.id for c in plan.candidates] == [parent.id]
    assert not domain.reads
    assert plan.diagnostics["event_source_candidates"] == 0
    assert plan.diagnostics["source_index_ids_returned"] == 0


@pytest.mark.parametrize("invalid", ["backlink", "filter"])
def test_event_hint_does_not_override_source_revalidation(monkeypatch, invalid):
    from jiuwen_memory.common.type_def import FilterClause, FilterOp

    domain, parent, children, query, policy, _, _ = event_reference_fixture()
    target = children[2]
    if invalid == "backlink":
        target.hierarchy.parent_id = "different-parent"
    else:
        parent.user_metadata["permitted"] = True
        target.user_metadata["permitted"] = False
        query.recheck_filters = FilterClause("user_metadata.permitted", FilterOp.EQ, True)
    domain.update(target.scope, [target])
    # A stale/broken backend hit must still fail the final body/link checks.
    monkeypatch.setattr(
        domain,
        "recall",
        lambda *a, **kw: RecallResult(
            batches=[RecallBatch(RecallChannel.KEYWORD, "l2", [ScoredUnit(target.id, 0.9)])]
        ),
    )
    plan = plan_relational(domain, policy, HOME, query, [ScoredMemoryUnit(parent, 1)])
    assert target.id not in {c.unit.id for c in plan.candidates}
    assert not plan.links
    assert plan.diagnostics["event_source_candidates"] == 0


def test_event_references_route_each_recursive_level_without_full_descent(monkeypatch):
    from jiuwen_memory.common.type_def import FilterClause

    domain, parent, children, query, policy, _, event = event_reference_fixture()
    root, decoy = deepcopy(parent), deepcopy(parent)
    root.id, decoy.id = "upper-root", "a-unrelated-summary"
    parent.hierarchy.parent_id = decoy.hierarchy.parent_id = root.id
    parent.hierarchy.parent_scope = decoy.hierarchy.parent_scope = deepcopy(HOME)
    root.hierarchy.child_ids = [decoy.id, parent.id]
    root.hierarchy.child_scopes = [deepcopy(HOME), deepcopy(HOME)]
    root.system_metadata.update(
        graphmem_height="2", graphmem_child_roles=[HierarchyRole.TIME_SPAN.value] * 2
    )
    root.system_metadata[FEATURES_KEY] = GraphSemantics(
        (EntityLabel("Alice", "person"),), (replace(event, sources=(1,)),)
    ).encode()
    domain.add(HOME, [root, decoy])
    domain.update(HOME, [parent])

    def ids_from(filters):
        if isinstance(filters, FilterClause):
            return filters.value if filters.field == "unit_id" else []
        return [uid for child in filters.children for uid in ids_from(child)]

    def recall(scope, parsed, *, channels, recall_limit):
        selected = sorted(ids_from(parsed.scalar_filters))[:recall_limit]
        return RecallResult(
            batches=[RecallBatch(channels[0], "l2", [ScoredUnit(uid, 0.8) for uid in selected])]
        )

    monkeypatch.setattr(domain, "recall", recall)
    domain.reads.clear()
    policy = replace(
        policy,
        branch_width=1,
        summary_candidate_limit=2,
        initial_candidate_limit=1,
        ancestor_hops=0,
    )
    plan = plan_relational(domain, policy, HOME, query, [ScoredMemoryUnit(root, 1)])
    selected = {c.unit.id for c in plan.candidates}
    assert {root.id, parent.id, children[2].id} <= selected
    assert decoy.id not in selected
    assert all(decoy.id not in ids for _, ids in domain.reads)
    assert plan.diagnostics["summary_candidates"] == 2
    assert plan.diagnostics["source_candidates"] == 2
    assert plan.diagnostics["event_reference_index_hits"] == 2


def test_summary_only_never_follows_an_event_hint_to_original_index_or_body(monkeypatch):
    domain, parent, _, query, policy, packer, _ = event_reference_fixture()

    def forbidden(*args, **kwargs):
        raise AssertionError("Summary-only mode followed an original event reference")

    monkeypatch.setattr(domain, "recall", forbidden)
    monkeypatch.setattr(domain, "get", forbidden)
    navigator = GraphEvidenceNavigator(domain, replace(policy, include_original=False))
    selected, bundle = navigator.pack_closure(
        HOME, query, [ScoredMemoryUnit(parent, 1)], packer, max_tokens=12000, top_k=8
    )
    assert [c.unit.id for c in selected] == [parent.id]
    assert bundle.navigation["event_source_candidates"] == 0


def test_conversation_day_filter_targets_messages_only_for_explicit_discussion():
    from jiuwen_memory.common.type_def import HierarchyKind
    from jiuwen_memory.retrieval.graphmem_query import (
        GraphMemParsedQuery,
        QueryNeeds,
        conversation_day_span,
    )

    needs = QueryNeeds(
        mode="temporal",
        facets=("topic of discussion",),
        analysis_status="used",
        time_from="2023-12-11",
        time_to="2023-12-12",
    )
    q = GraphMemParsedQuery(
        raw="What is the topic of discussion between Alice and Bob on 11 December 2023?",
        hierarchy_kind=HierarchyKind.TIME,
        navigation_needs=needs,
    )
    start, end = conversation_day_span(q)
    assert start.isoformat().startswith("2023-12-11T00:00")
    assert end.isoformat().startswith("2023-12-11T23:59:59")
    q.raw = "What did Alice buy on 11 December 2023?"
    assert conversation_day_span(q) is None
    q.raw = "What did Alice and Bob discuss on 11 December 2023?"
    assert conversation_day_span(q) == (start, end)
    q.raw = "What did Alice and Bob discuss on 12 December 2023?"
    assert conversation_day_span(q) is None


def test_relation_source_exchange_preserves_ranked_and_attributed_candidates():
    from collections import deque

    from jiuwen_memory.retrieval.graphmem_relational_navigation import _swap_unranked_adjacent

    weak = deque(["seed", "neighbor"])
    protected = deque(["event", "detail", "keyword"])
    groups = deque(
        [
            (None, None, weak, {"seed": 1}, {}, {}, {}, 0),
            (None, None, protected, {"keyword": 1}, {}, {"event": [0]}, {"detail": [0]}, 0),
        ]
    )
    assert _swap_unranked_adjacent(groups) == (weak, "neighbor")
    assert list(weak) == ["seed", "neighbor"]
    weak.pop()
    assert _swap_unranked_adjacent(groups) is None


def test_explicit_keyword_channel_skips_relation_port(monkeypatch):
    domain, parents, _, query, policy, packer = fixture()

    def unexpected_edges(*args, **kwargs):
        pytest.fail("explicit keyword-only request must not read graph relation edges")

    monkeypatch.setattr(domain, "related_edges", unexpected_edges)
    _, bundle = GraphEvidenceNavigator(domain, policy).pack_closure(
        HOME, replace(query, vector=[1.0]), [ScoredMemoryUnit(parents[0], 1)], packer,
        max_tokens=12000, top_k=16, recall_channels=[RecallChannel.KEYWORD],
    )
    assert bundle.navigation["neighbors_added"] == 0
    assert bundle.sources


def test_secondary_probe_intersects_channels_and_skips_empty_intersection(monkeypatch):
    from jiuwen_memory.retrieval.graphmem_request_reads import _RequestReads

    domain, _, _, query, policy, _ = fixture()
    original = domain.recall
    calls = []

    def recall(scope, parsed, *, channels, recall_limit):
        calls.append(channels)
        return original(scope, parsed, channels=channels, recall_limit=recall_limit)

    monkeypatch.setattr(domain, "recall", recall)
    reads = _RequestReads(domain, policy, [], recall_channels=[RecallChannel.KEYWORD])
    reads.recall(HOME, query, channels=[RecallChannel.VECTOR], recall_limit=2)
    reads.recall(
        HOME, query, channels=[RecallChannel.VECTOR, RecallChannel.KEYWORD], recall_limit=2,
    )
    assert calls == [[RecallChannel.KEYWORD]]
