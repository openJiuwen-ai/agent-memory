"""Bounded semantic relations on recursive session trees, using the real edge writer."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.graphmem_semantics import (
    FEATURES_KEY,
    EntityLabel,
    EventTime,
    GraphSemantics,
    SceneEvent,
)
from jiuwen_memory.common.type_def import HierarchyRole, LifecycleState
from jiuwen_memory.construction.associator_impl.graphmem_topdown import (
    TopDownGraphMemAssociator,
    TopDownRelationPolicy,
)
from tests.unit.construction.graphmem_fixtures import HOME, Words, sync, unit, world

pytestmark = pytest.mark.unit


def summary(uid, session, text="topic", *, name="Alice", height=1, parent="", window=None):
    node = unit(uid, text, entities=[name] if name else [], parent=parent)
    node.hierarchy.role = HierarchyRole.TIME_SPAN
    node.system_metadata.update(
        {
            "graphmem_recursive_version": "1",
            "graphmem_height": str(height),
            "graphmem_session_scope": json.dumps(
                (HOME.org, HOME.space, HOME.user, HOME.agent, session)
            ),
        }
    )
    entity = EntityLabel(name, "person") if name else None
    events = (
        (SceneEvent(text, (entity.key,) if entity else (), "completed", window, (0,)),)
        if window
        else ()
    )
    node.system_metadata[FEATURES_KEY] = GraphSemantics(
        (entity,) if entity else (), events
    ).encode()
    return node


def attach(parent, children):
    parent.hierarchy.child_ids = [c.id for c in children]
    parent.hierarchy.child_scopes = [deepcopy(c.scope) for c in children]
    parent.system_metadata["graphmem_child_roles"] = [c.hierarchy.role.value for c in children]
    for child in children:
        child.hierarchy.parent_id = parent.id
        child.hierarchy.parent_scope = deepcopy(parent.scope)


def day(date, *, precision="day", kind="occurrence"):
    return EventTime(
        f"2023-05-{date:02d}T00:00:00Z", f"2023-05-{date + 1:02d}T00:00:00Z", precision, kind
    )


def test_common_speaker_alone_does_not_form_a_clique():
    nodes = [summary(str(i), f"s{i}", f"distinct_topic_{i}") for i in range(8)]
    result = TopDownGraphMemAssociator(Words()).associate_delta(nodes, {})
    assert not result.relations
    assert result.stats["model_calls"] == 0
    assert result.stats["selected_edges"] == 0


def test_event_interval_not_conversation_date_and_unknown_times_do_not_link():
    nodes = [
        summary("a", "s1", "cycling", window=day(8)),
        summary("b", "s2", "bicycle", window=day(9)),
        summary("c", "s3", "astronomy", window=EventTime()),
        summary("d", "s4", "music", window=day(8, kind="recurrence")),
    ]
    # Message/coverage dates are equal for every node in this fixture, but only
    # the two attributed event intervals can produce the temporal link.
    result = TopDownGraphMemAssociator(Words()).associate_delta(nodes, {})
    assert [(r.source_id, r.target_id) for r in result.relations] == [("a", "b")]
    assert result.relations[0].metadata["attributes"] == ["entity", "temporal"]
    assert result.relations[0].metadata["matched_events"][0]["gap_seconds"] == 0
    nodes[1].system_metadata[FEATURES_KEY] = GraphSemantics(
        (EntityLabel("Alice", "person"),),
        (
            SceneEvent(
                "bicycle",
                (EntityLabel("Alice", "person").key,),
                "completed",
                EventTime("2023-01-01", "2024-01-01", "year", "uncertain"),
                (0,),
            ),
        ),
    ).encode()
    assert not TopDownGraphMemAssociator(Words()).associate(nodes)


def test_top_down_refines_related_roots_and_handles_unequal_height():
    nodes = [
        summary("p1", "s1", name="", height=3),
        summary("p2", "s2", name="", height=2),
        summary("a", "s1", name="", height=2),
        summary("b", "s2", name=""),
        summary("a1", "s1", name=""),
        summary("other", "s3", name="", height=2),
        summary("unopened", "s3", name=""),
    ]
    p1, p2, a, b, a1, other, unopened = nodes
    attach(p1, [a])
    attach(a, [a1])
    attach(p2, [b])
    attach(other, [unopened])
    vectors = {u.id: ([0.0, 1.0] if u.id in {"other", "unopened"} else [1.0, 0.0]) for u in nodes}
    associator = TopDownGraphMemAssociator(Words(), indexed_vectors=lambda u: vectors[u.id])
    result = associator.associate_delta(nodes, {})
    pairs = {(r.source_id, r.target_id) for r in result.relations}
    assert {("p1", "p2"), ("a", "b"), ("a1", "b")} <= pairs
    assert all("other" not in pair and "unopened" not in pair for pair in pairs)
    assert result.stats["refined_pairs"] == 2
    assert result.stats["pair_checks"] < len(nodes) * (len(nodes) - 1) // 2


def test_coarse_parent_candidate_can_reveal_a_strong_child_relation_without_parent_edge():
    nodes = [
        summary("p1", "s1", name="", height=2),
        summary("p2", "s2", name="", height=2),
        summary("a", "s1", name=""),
        summary("b", "s2", name=""),
    ]
    attach(nodes[0], [nodes[2]])
    attach(nodes[1], [nodes[3]])
    vectors = {"p1": [1.0, 0.0], "p2": [0.7, 0.7141428429], "a": [0.0, 1.0], "b": [0.0, 1.0]}
    result = TopDownGraphMemAssociator(
        Words(), indexed_vectors=lambda u: vectors[u.id]
    ).associate_delta(nodes, {})
    pairs = {(r.source_id, r.target_id) for r in result.relations}
    assert pairs == {("a", "b")}
    assert result.stats["coarse_routing_pairs"] == 1
    assert result.stats["refined_pairs"] == 1
    assert result.stats["model_calls"] == 0


def test_weak_parents_do_not_open_unbounded_refinement_or_persist_unproved_edges():
    roots = [summary(f"p{i}", f"s{i}", name="", height=2) for i in range(10)]
    leaves = [summary(f"c{i}", f"s{i}", name="") for i in range(10)]
    for parent, child in zip(roots, leaves, strict=True):
        attach(parent, [child])
    nodes = roots + leaves
    vectors = {}
    for i, node in enumerate(nodes):
        # Roots have moderately similar overviews; their children are unrelated.
        v = [0.0] * 21
        if i < 10:
            v[0] = 0.81
        v[i + 1] = 0.59 if i < 10 else 1.0
        vectors[node.id] = v
    policy = TopDownRelationPolicy(root_neighbors=1, child_neighbors=1, max_pairs=12)
    result = TopDownGraphMemAssociator(
        Words(), policy, indexed_vectors=lambda u: vectors[u.id]
    ).associate_delta(nodes, {})
    assert not result.relations
    assert result.stats["pair_checks"] <= policy.max_pairs
    assert result.stats["vector_scores"] <= policy.max_pairs
    assert result.stats["routing_pairs"] <= policy.max_pairs
    assert result.stats["limited_pairs"] > 0


def test_coarse_cosine_does_not_evict_an_already_supported_time_neighbor():
    roots = [
        summary("pa", "s1", height=2, window=day(8)),
        summary("pb", "s2", height=2, window=day(11)),
        summary("pc", "s3", height=2),
    ]
    leaves = [summary(f"c{i}", f"s{i + 1}", name="") for i in range(3)]
    for parent, child in zip(roots, leaves, strict=True):
        attach(parent, [child])
    vectors = {
        "pa": [1.0, 0.0, 0.0],
        "pb": [0.4, 0.916515, 0.0],
        "pc": [0.7, 0.7, 0.14],
        "c0": [1.0, 0.0, 0.0],
        "c1": [0.0, 1.0, 0.0],
        "c2": [0.0, 0.0, 1.0],
    }
    policy = TopDownRelationPolicy(root_neighbors=1, common_entity_sessions=1)
    result = TopDownGraphMemAssociator(
        Words(), policy, indexed_vectors=lambda u: vectors[u.id]
    ).associate_delta(roots + leaves, {})
    pairs = {(r.source_id, r.target_id) for r in result.relations}
    assert ("pa", "pb") in pairs
    assert ("pa", "pc") not in pairs


def test_rare_entity_can_recover_a_topic_omitted_by_parent():
    nodes = [
        summary("p1", "s1", name="", height=2),
        summary("p2", "s2", name="", height=2),
        summary("a", "s1", name="Zelda"),
        summary("b", "s2", name="Zelda"),
    ]
    attach(nodes[0], [nodes[2]])
    attach(nodes[1], [nodes[3]])
    result = TopDownGraphMemAssociator(Words()).associate_delta(nodes, {})
    assert {(r.source_id, r.target_id) for r in result.relations} == {("a", "b")}
    assert result.stats["rare_entity_backstops"] == 1


def test_degree_is_bounded_and_noop_append_archive_write_correct_incident_edges():
    nodes = [summary(f"a{i}", f"s{i}", name="") for i in range(7)]
    policy = TopDownRelationPolicy(max_degree=2, max_edges=3)
    associator = TopDownGraphMemAssociator(Words(), policy, indexed_vectors=lambda u: [1.0, 0.0])
    _, graph, _, writer = world(nodes)
    first = sync(associator, writer, nodes)
    assert first.stats["max_degree"] == 2
    assert first.stats["selected_edges"] == 3
    old = graph.get_edges(HOME, [u.id for u in nodes], relation="graphmem")
    assert sync(associator, writer, list(reversed(nodes))).affected_ids == []
    extra = summary("new", "s_new", "unrelated", name="")
    # No matching projection: an isolated append should not rewrite existing edges.
    isolating = TopDownGraphMemAssociator(
        Words(), policy, indexed_vectors=lambda u: [0.0, 1.0] if u.id == "new" else [1.0, 0.0]
    )
    delta = sync(isolating, writer, [*nodes, extra])
    assert delta.affected_ids == ["new"]
    assert graph.get_edges(HOME, [u.id for u in nodes], relation="graphmem") == old
    nodes[0].lifecycle = LifecycleState.FORGOTTEN
    sync(isolating, writer, [*nodes, extra])
    assert not graph.get_edges(HOME, [nodes[0].id], relation="graphmem")


def test_scope_mixing_and_stale_projection_fail_or_skip():
    nodes = [summary("a", "s1"), summary("b", "s2")]
    nodes[1].scope = replace(HOME, user="another")
    with pytest.raises(ValidationError, match="Scope"):
        TopDownGraphMemAssociator(Words()).associate(nodes)
    assert TopDownGraphMemAssociator(Words()).associate([]) == []


@pytest.mark.parametrize("value", [None, True, 1, "other", "L2"])
def test_projection_choice_rejects_ambiguous_values(value):
    from jiuwen_memory.construction.associator_impl.graphmem_topdown import _build

    with pytest.raises(ValidationError, match="vector_view"):
        TopDownGraphMemAssociator(Words(), vector_view=value)
    with pytest.raises(ValidationError, match="vector_view"):
        _build({"vector_view": value})


def test_factory_projection_reuses_stored_vectors_and_refreshes_relation_cache(monkeypatch):
    import hashlib
    from types import SimpleNamespace

    from jiuwen_memory.common.feature_extractor.base import FeatureExtractorProducer
    from jiuwen_memory.config.context import AssemblyContext, ComponentConfig
    from jiuwen_memory.construction.associator_impl.graphmem_topdown import _build
    from jiuwen_memory.construction.index_builder_impl._index_ops import layer_record_id
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
    from jiuwen_memory.storage.store_manager import StoreManagerProducer
    from jiuwen_memory.storage.types import VectorRecord
    from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

    nodes = [summary("a", "s1", "cycling", name=""), summary("b", "s2", "bicycle", name="")]
    kv, body, scene = InMemoryKVStore(), InMemoryVectorStore(), InMemoryVectorStore()
    for i, node in enumerate(nodes):
        node.layers.l1 = "scene " + node.id
        cid = node.id + "-0"
        kv.insert(HOME, "/index/chunks/" + node.id, json.dumps([cid]).encode())
        body.insert(
            HOME,
            [
                VectorRecord(
                    cid,
                    [1.0, 0.0],
                    {
                        "unit_id": node.id,
                        "indexed_text_sha256": hashlib.sha256(node.content.encode()).hexdigest(),
                    },
                )
            ],
        )
        scene.insert(
            HOME,
            [
                VectorRecord(
                    layer_record_id(node.id, "l1"),
                    [1.0, 0.0] if i == 0 else [0.0, 1.0],
                    {
                        "unit_id": node.id,
                        "indexed_text_sha256": hashlib.sha256(node.layers.l1.encode()).hexdigest(),
                    },
                )
            ],
        )
    manager = SimpleNamespace(
        has_vector=lambda name: True,
        has_kv=lambda name: True,
        vector=lambda name: scene if name == "layers_l1" else body,
        kv=lambda name: kv,
    )
    monkeypatch.setattr(StoreManagerProducer, "resolve", lambda *a, **k: manager)
    monkeypatch.setattr(FeatureExtractorProducer, "dep", lambda *a, **k: Words())
    ctx = AssemblyContext.from_dict({})
    compact = _build(ComponentConfig({}, ctx))
    detailed = _build(ComponentConfig({"vector_view": "detail"}, ctx))
    original = deepcopy(nodes)
    _, graph, _, writer = world(nodes)
    assert not sync(compact, writer, nodes).relations
    delta = sync(detailed, writer, nodes)
    assert delta.affected_ids == ["a", "b"]
    assert [(r.source_id, r.target_id) for r in delta.relations] == [("a", "b")]
    assert graph.get_edges(HOME, ["a"])[0].properties["attributes"] == ["semantic"]
    assert compact.policy_id == TopDownGraphMemAssociator(Words()).policy_id
    assert compact.policy_id != detailed.policy_id
    assert sync(detailed, writer, nodes).affected_ids == []
    sync(compact, writer, nodes)
    assert graph.get_edges(HOME, ["a"]) == []
    assert nodes == original and all(not u.vectors for u in nodes)


@pytest.mark.parametrize(
    "params", [{"max_degree": True}, {"max_pairs": 0}, {"semantic_threshold": float("nan")}]
)
def test_invalid_policy_is_rejected(params):
    with pytest.raises(ValidationError):
        TopDownRelationPolicy(**params)


def test_isolated_semantic_backstop_reuses_pairs_and_preserves_initial_relations():
    nodes = [
        summary("a", "s1", "orchard event", name=""),
        summary("b", "s2", "fruit harvest", name=""),
        summary("c", "s3", "hardware", name=""),
        summary("d", "s4", "components", name=""),
    ]
    vectors = {
        "a": [1.0, 0.0, 0.0],
        "b": [0.76, 0.6499230724, 0.0],
        "c": [0.0, 0.0, 1.0],
        "d": [0.03, 0.0, 0.9995499],
    }
    baseline = TopDownRelationPolicy(root_neighbors=1, max_degree=1, max_edges=2)
    reference = TopDownGraphMemAssociator(
        Words(), baseline, indexed_vectors=lambda u: vectors[u.id]
    ).associate_delta(nodes, {})
    profile = TopDownGraphMemAssociator(
        Words(),
        replace(baseline, isolated_semantic_floor=0.72),
        indexed_vectors=lambda u: vectors[u.id],
    )
    trial = profile.associate_delta(nodes, {})
    assert {(edge.source_id, edge.target_id) for edge in reference.relations} == {("c", "d")}
    assert {(edge.source_id, edge.target_id) for edge in trial.relations} == {
        ("a", "b"),
        ("c", "d"),
    }
    extra = next(edge for edge in trial.relations if edge.source_id == "a")
    assert extra.metadata["stage"] == "isolated_semantic_backstop"
    assert extra.metadata["attributes"] == ["semantic"] and not extra.metadata["matched_events"]
    assert not extra.metadata["shared_entities"]
    assert trial.stats["isolated_semantic_backstop_edges"] == 1
    for field in ("pair_checks", "vector_scores", "model_calls"):
        assert trial.stats[field] == reference.stats[field]

    _, graph, _, writer = world(nodes)
    sync(profile, writer, nodes)
    original_edges = graph.get_edges(HOME, [u.id for u in nodes], relation="graphmem")
    extra_unit = summary("raw", "s4", "appended unrelated material", height=0, name="")
    delta = sync(profile, writer, [*nodes, extra_unit])
    assert delta.affected_ids == ["raw"]
    assert graph.get_edges(HOME, [u.id for u in nodes], relation="graphmem") == original_edges
    assert sync(profile, writer, list(reversed([*nodes, extra_unit]))).affected_ids == []


@pytest.mark.parametrize("floor", [-0.01, True, float("nan"), 0.78, 0.99])
def test_isolated_backstop_similarity_floor_has_explicit_bounds(floor):
    with pytest.raises(ValidationError, match="isolated_semantic_floor"):
        TopDownRelationPolicy(isolated_semantic_floor=floor)


def test_isolated_backstop_is_disabled_when_mean_ordinary_degree_is_at_least_one():
    from math import cos, radians, sin

    nodes = [summary(uid, f"session-{uid}", name="") for uid in ("a", "b", "c", "solo")]
    angles = {"a": 0, "b": 30, "c": 60, "solo": -42}
    vectors = {uid: [cos(radians(angle)), sin(radians(angle))] for uid, angle in angles.items()}
    policy = TopDownRelationPolicy(isolated_semantic_floor=0.72)
    enriched = TopDownGraphMemAssociator(
        Words(), policy, indexed_vectors=lambda u: vectors[u.id]
    ).associate_delta(nodes, {})
    assert enriched.stats["ordinary_relation_edges"] == 2
    assert enriched.stats["relation_summary_nodes"] == 4
    assert {(rel.source_id, rel.target_id) for rel in enriched.relations} == {
        ("a", "b"),
        ("b", "c"),
    }
    assert "isolated_semantic_backstop_edges" not in enriched.stats


def test_append_crossing_sparsity_boundary_retires_only_obsolete_relations():
    nodes = [summary(uid, f"session-{uid}", name="") for uid in ("a", "b", "c", "d", "unrelated")]
    vectors = {
        "a": [0.0, 0.0, 1.0, 0.0],
        "b": [0.0, 0.0, 1.0, 0.0],
        "c": [1.0, 0.0, 0.0, 0.0],
        "d": [0.75, 0.6614378278, 0.0, 0.0],
        "unrelated": [0.0, 0.0, 0.0, 1.0],
        "new": [0.0, 0.0, 1.0, 0.0],
    }
    policy = TopDownRelationPolicy(isolated_semantic_floor=0.72)
    assoc = TopDownGraphMemAssociator(Words(), policy, indexed_vectors=lambda u: vectors[u.id])
    _, graph, _, writer = world(nodes)

    initial = sync(assoc, writer, nodes)
    assert initial.stats["isolated_semantic_backstop_edges"] == 1
    old = {
        frozenset((e.source, e.target)): e
        for e in graph.get_edges(HOME, [u.id for u in nodes], relation="graphmem")
    }
    assert set(old) == {frozenset(("a", "b")), frozenset(("c", "d"))}

    new = summary("new", "session-new", name="")
    active = [*nodes, new]
    delta = sync(assoc, writer, active)
    assert (
        delta.stats["ordinary_relation_edges"] == 3 and delta.stats["relation_summary_nodes"] == 6
    )
    assert set(delta.affected_ids) == {"a", "b", "c", "d", "new"}
    revised = {
        frozenset((e.source, e.target)): e
        for e in graph.get_edges(HOME, [u.id for u in active], relation="graphmem")
    }
    assert set(revised) == {frozenset(("a", "b")), frozenset(("a", "new")), frozenset(("b", "new"))}
    assert revised[frozenset(("a", "b"))] == old[frozenset(("a", "b"))]
    assert sync(assoc, writer, active).affected_ids == []
