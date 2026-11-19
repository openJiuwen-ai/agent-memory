"""Relationship exploration leaves bounded room for selected trees to descend."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.graph_memory import memory_fingerprint
from jiuwen_memory.common.graphmem_semantics import FEATURES_KEY, EntityLabel, GraphSemantics
from jiuwen_memory.common.graphmem_structure import RECURSIVE_VERSION
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, ScoredMemoryUnit
from jiuwen_memory.retrieval import graphmem_relational_navigation as navigation
from jiuwen_memory.retrieval.graph_navigation import GraphNavigationPolicy
from jiuwen_memory.retrieval.graphmem_query import GraphMemParsedQuery, QueryNeeds
from tests.unit.construction.graphmem_fixtures import HOME, tree, world

pytestmark = pytest.mark.unit


def fixture(monkeypatch, *, enabled, summary_only=False, empty_child_index=False):
    child, raw = tree("child", "session", text="Alice riverside picnic", raw="Alice brought tea.")
    root, _ = tree("root", "session", text="Alice riverside picnic")
    root.hierarchy.child_ids = [child.id]
    root.hierarchy.child_scopes = [deepcopy(HOME)]
    child.hierarchy.parent_id = root.id
    child.hierarchy.parent_scope = deepcopy(HOME)
    session = json.dumps((HOME.org, HOME.space, HOME.user, HOME.agent, raw.scope.session))
    for parent, height, role in ((root, 2, HierarchyRole.TIME_SPAN),
                                (child, 1, HierarchyRole.SNAPSHOT)):
        parent.system_metadata.update(graphmem_recursive_version=RECURSIVE_VERSION,
            graphmem_height=str(height), graphmem_session_scope=session,
            graphmem_child_roles=[role.value])
        parent.system_metadata[FEATURES_KEY] = GraphSemantics(
            (EntityLabel("Alice", "person"),), ()).encode()
    noise = [tree(f"noise{i}", f"other{i}", text="Bob mountain climbing")[0]
             for i in range(3)]
    for parent in noise:
        parent.system_metadata[FEATURES_KEY] = GraphSemantics(
            (EntityLabel("Bob", "person"),), ()).encode()
    domain, _, _, _ = world([root, child, raw, *noise])
    edges = [SimpleNamespace(id=f"edge{i}", source=root.id, target=unit.id, properties={
        "attributes": ["semantic"], "score": 0.8,
        "fingerprints": {root.id: memory_fingerprint(root), unit.id: memory_fingerprint(unit)},
    }) for i, unit in enumerate(noise)]
    monkeypatch.setattr(domain, "related_edges", lambda *args, **kwargs: edges)
    index_roles = []

    def indexed(cache, query, target_scope, ids, role, limit, needs, *args, **kwargs):
        index_roles.append(role)
        if empty_child_index and ids == [child.id]:
            return {}, {}, None
        return {uid: 1 / (i + 1) for i, uid in enumerate(ids[:limit])}, {}, None

    monkeypatch.setattr(navigation, "_index_candidates", indexed)
    query = GraphMemParsedQuery(raw="What did Alice bring to the riverside picnic?",
        hierarchy_kind=HierarchyKind.TIME, hierarchy_role=HierarchyRole.TIME_SPAN,
        navigation_needs=QueryNeeds(entities=("Alice",), facets=("riverside picnic",)))
    policy = GraphNavigationPolicy(strategy="relational", seed_count=1, max_hops=2,
        max_neighbors=3, branch_width=1, ancestor_hops=0, max_reads=8,
        include_original=not summary_only, initial_candidate_limit=1,
        summary_candidate_limit=4, source_candidate_limit=2,
        reserve_descendant_probes=enabled)
    plan = navigation.plan_relational(domain, policy, HOME, query, [ScoredMemoryUnit(root, 1)])
    return plan, domain, index_roles, root, child, raw


def test_failed_relation_probes_cannot_consume_selected_tree_descent_slots(monkeypatch):
    old, _, _, _, child, raw = fixture(monkeypatch, enabled=False)
    new, domain, _, _, _, _ = fixture(monkeypatch, enabled=True)
    assert child.id not in {c.unit.id for c in old.candidates}
    assert {child.id, raw.id} <= {c.unit.id for c in new.candidates}
    stage = new.diagnostics["relation_probe_stages"][0]
    assert stage["descendant_probe_reserve"] == 1
    assert stage["dead_end_fallback"] and stage["new_probes"] == 2
    assert new.diagnostics["summary_ids_probed"] == 4
    assert new.diagnostics["source_candidates"] == 1
    assert sum(len(ids) for _, ids in domain.reads) <= 8
    assert "descendant_probe_reserve" not in old.diagnostics["relation_probe_stages"][0]


def test_summary_only_preserves_descent_without_accessing_originals(monkeypatch):
    plan, domain, roles, _, child, raw = fixture(monkeypatch, enabled=True, summary_only=True)
    assert child.id in {c.unit.id for c in plan.candidates}
    assert HierarchyRole.SNAPSHOT not in roles
    assert all(scope != raw.scope for scope, _ in domain.reads)
    assert plan.diagnostics["source_candidates"] == 0


def test_reservation_does_not_override_empty_child_index(monkeypatch):
    plan, domain, _, _, child, _ = fixture(monkeypatch, enabled=True, empty_child_index=True)
    assert child.id not in {c.unit.id for c in plan.candidates}
    assert all(child.id not in ids for _, ids in domain.reads)
    assert plan.diagnostics["summary_ids_probed"] == 3


def test_reserve_uses_full_scope_dedup_and_existing_branch_width(monkeypatch):
    _, _, _, root, child, raw = fixture(monkeypatch, enabled=True)
    item = ScoredMemoryUnit(root, 1)
    reserve = navigation._descendant_probe_reserve
    key = navigation._RequestReads.key
    assert reserve([item, item], set(), HOME, 2, 10) == 1
    assert reserve([item], {key(child.scope, child.id)}, HOME, 2, 10) == 0
    assert reserve([item], {key(raw.scope, child.id)}, HOME, 2, 10) == 1
    root.hierarchy.child_ids *= 5
    root.hierarchy.child_scopes *= 5
    root.system_metadata["graphmem_child_roles"] *= 5
    assert reserve([item], set(), HOME, 2, 10) == 1
    root.hierarchy.child_ids = [f"child{i}" for i in range(5)]
    assert reserve([item], set(), HOME, 2, 10) == 2
    assert reserve([item], set(), HOME, 2, 1) == 1
    assert reserve([item], set(), HOME, 2, 0) == 0


def test_reserve_skips_raw_children_bad_metadata_and_other_scopes(monkeypatch):
    _, _, _, root, child, _ = fixture(monkeypatch, enabled=True)
    reserve = navigation._descendant_probe_reserve
    assert reserve([ScoredMemoryUnit(child, 1)], set(), HOME, 2, 10) == 0
    root.hierarchy.child_scopes = [replace(HOME, user="bob")]
    assert reserve([ScoredMemoryUnit(root, 1)], set(), HOME, 2, 10) == 0
    root.hierarchy.child_scopes = [deepcopy(HOME)]
    root.system_metadata["graphmem_child_roles"] = []
    assert reserve([ScoredMemoryUnit(root, 1)], set(), HOME, 2, 10) == 0


@pytest.mark.parametrize("invalid", [1, 0, "true", None])
def test_reserve_setting_rejects_non_booleans(invalid):
    with pytest.raises(ValidationError, match="reserve_descendant_probes"):
        GraphNavigationPolicy(strategy="relational", reserve_descendant_probes=invalid)


def test_reserve_is_opt_in_and_requires_relational_navigation():
    assert not GraphNavigationPolicy().reserve_descendant_probes
    with pytest.raises(ValidationError, match="supports relational"):
        GraphNavigationPolicy(strategy="legacy", reserve_descendant_probes=True)
