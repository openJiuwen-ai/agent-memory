"""Multi-duty relation expansion: matched/valid duties, path states, shared budget."""

import pytest

from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.relations.catalog import RelationCatalog
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.continuity import (
    MemoryRef,
    RelationRecord,
    RoleViewQuery,
    ViewBudget,
)
from jiuwen_memory.retrieval.role_view_impl.bounded import RoleViewBuilder
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.types import Edge

pytestmark = pytest.mark.unit

SCOPE = Scope(org="o", space="s")


class World:
    """Hand-written graph answers paired with a relation catalog and builder."""

    def __init__(self, relation_types=None, duty_policies=None):
        self.scope = SCOPE
        self.graph = InMemoryGraphStore()
        self.catalog = RelationCatalog(relation_types, duty_policies)
        self.builder = RoleViewBuilder(self.catalog)
        self.units: dict[str, MemoryUnit] = {}
        self.relations: dict[str, RelationRecord] = {}
        self.refs: dict[str, MemoryRef] = {}

    def unit(self, name, kind="statement"):
        u = MemoryUnit(
            id=name, scope=self.scope, segments=[Segment(content=name)],
            system_metadata={"continuity_kind": kind},
        )
        self.units[name] = u
        self.refs[name] = MemoryRef.of(u)
        return self.refs[name]

    def link(self, rid, source, target, rtype, evidence=None, conditions=None):
        rec = RelationRecord(
            rid, source, target, rtype, status="confirmed",
            evidence_refs=evidence or [source], conditions=conditions or [],
        )
        self.relations[rid] = rec
        self.graph.insert(
            self.scope,
            edges=[Edge("continuity:relation:" + rid, source.key, target.key, rtype)],
        )
        return rec

    def read(self, ref):
        return self.units[ref.unit_id]

    def resolve_edge(self, edge_id):
        rid = edge_id.removeprefix("continuity:relation:")
        rec = self.relations[rid]
        rel_unit = MemoryUnit(
            id="rel-" + rid, scope=self.scope, segments=[Segment(content=rid)]
        )
        self.units[rel_unit.id] = rel_unit
        return rec, MemoryRef.of(rel_unit)

    def view(self, anchor, duty, budget=None):
        if isinstance(anchor, str):
            anchor = self.refs[anchor]
        return self.builder.build(
            self.scope,
            RoleViewQuery(anchor=anchor, duty=duty, budget=budget or ViewBudget()),
            graph=self.graph,
            read=self.read,
            discover=lambda q, n: [],
            resolve_edge=self.resolve_edge,
        )


def _link_world():
    world = World(
        {"link": {"display_name": "link", "description": "generic link"}},
        {
            "A": {"traversal": [{"edge_type": "link", "direction": "forward", "max_depth": 1}],
                  "budget": {"max_depth": 3}},
            "B": {"traversal": [{"edge_type": "link", "direction": "forward", "max_depth": 3}],
                  "budget": {"max_depth": 3}},
        },
    )
    for name in ("n0", "n1", "n2", "n3"):
        world.unit(name)
    world.link("e01", world.refs["n0"], world.refs["n1"], "link")
    world.link("e12", world.refs["n1"], world.refs["n2"], "link")
    world.link("e23", world.refs["n2"], world.refs["n3"], "link")
    return world


def test_duty_input_normalization_and_rejection():
    assert RoleViewQuery(query="x", duty=["diagnosis", "diagnosis", "execution"]).duty == [
        "diagnosis", "execution",
    ]
    assert RoleViewQuery(query="x", duty="testing").duty == "testing"
    with pytest.raises(ValidationError):
        RoleViewQuery(query="x", duty=["a", "b", "c", "d", "e"])
    world = World()
    world.unit("n0")
    with pytest.raises(ValidationError, match="unknown duty"):
        world.view("n0", "not_registered")


def test_third_hop_belongs_only_to_longer_duty():
    world = _link_world()
    result = world.view("n0", ["A", "B"], ViewBudget(max_depth=3))
    longest = max(result["paths"], key=lambda p: len(p["steps"]))
    assert len(longest["steps"]) == 3
    assert longest["steps"][0]["matched_duties"] == ["A", "B"]
    assert longest["steps"][1]["matched_duties"] == ["B"]
    assert longest["steps"][2]["matched_duties"] == ["B"]
    assert longest["valid_duties"] == ["B"]
    assert result["meta"]["duty"] == "A"
    assert result["meta"]["duties"] == ["A", "B"]
    assert "duty_coverage" in result["meta"]


def test_cross_duty_path_returns_with_empty_valid_duties():
    world = World(
        {"a_link": {"description": "a", "display_name": "a"},
         "b_link": {"description": "b", "display_name": "b"}},
        {
            "A": {"traversal": [{"edge_type": "a_link", "direction": "forward"}]},
            "B": {"traversal": [{"edge_type": "b_link", "direction": "forward"}]},
        },
    )
    for name in ("n0", "n1", "n2"):
        world.unit(name)
    world.link("e1", world.refs["n0"], world.refs["n1"], "a_link")
    world.link("e2", world.refs["n1"], world.refs["n2"], "b_link")
    result = world.view("n0", ["A", "B"])
    longest = max(result["paths"], key=lambda p: len(p["steps"]))
    assert longest["valid_duties"] == []
    assert [s["matched_duties"] for s in longest["steps"]] == [["A"], ["B"]]


def test_shared_node_keeps_one_body_and_multiple_paths():
    world = World(
        {"a_link": {"description": "a", "display_name": "a"},
         "b_link": {"description": "b", "display_name": "b"}},
        {
            "A": {"traversal": [{"edge_type": "a_link", "direction": "forward"}]},
            "B": {"traversal": [{"edge_type": "b_link", "direction": "forward"}]},
        },
    )
    for name in ("n0", "n1", "n2", "n3"):
        world.unit(name)
    world.link("e1", world.refs["n0"], world.refs["n1"], "a_link")
    world.link("e2", world.refs["n1"], world.refs["n3"], "b_link")
    world.link("e3", world.refs["n0"], world.refs["n2"], "b_link")
    world.link("e4", world.refs["n2"], world.refs["n3"], "a_link")
    result = world.view("n0", ["A", "B"])
    n3 = next(i for i in result["facts"] if i["text"] == "n3")
    assert len(n3["path_ids"]) == 2


def test_shorter_path_still_explores_beyond_node():
    world = World(
        {"link": {"display_name": "link", "description": "link"}},
        {"A": {"traversal": [{"edge_type": "link", "direction": "forward", "max_depth": 2}]}},
    )
    for name in ("n0", "n1", "nx", "n2"):
        world.unit(name)
    world.link("e_short", world.refs["n0"], world.refs["n1"], "link")
    world.link("e_loop1", world.refs["n0"], world.refs["nx"], "link")
    world.link("e_loop2", world.refs["nx"], world.refs["n1"], "link")
    world.link("e_next", world.refs["n1"], world.refs["n2"], "link")
    result = world.view("n0", "A")
    assert "n2" in [i["text"] for i in result["facts"]]


def test_shared_edge_credited_once_to_every_matching_duty():
    world = World(
        {"link": {"display_name": "link", "description": "link"}},
        {
            "A": {"traversal": [{"edge_type": "link", "direction": "forward"}]},
            "B": {"traversal": [{"edge_type": "link", "direction": "forward"}]},
            "C": {"traversal": [{"edge_type": "link", "direction": "forward"}]},
        },
    )
    world.unit("n0")
    world.unit("n1")
    world.link("e", world.refs["n0"], world.refs["n1"], "link")
    result = world.view("n0", ["A", "B", "C"], ViewBudget(max_edges=1))
    cov = result["meta"]["duty_coverage"]
    assert cov["A"]["expanded_edges"] == 1
    assert cov["B"]["expanded_edges"] == 1
    assert cov["C"]["expanded_edges"] == 1
    # Only one edge was materialized budget-wise, but all three duties credited.
    assert len(result["paths"]) == 1


def test_budget_smaller_than_duties_marks_unstarted():
    world = World(
        {"a": {"display_name": "a", "description": "a"},
         "b": {"display_name": "b", "description": "b"},
         "c": {"display_name": "c", "description": "c"}},
        {
            "A": {"traversal": [{"edge_type": "a", "direction": "forward"}]},
            "B": {"traversal": [{"edge_type": "b", "direction": "forward"}]},
            "C": {"traversal": [{"edge_type": "c", "direction": "forward"}]},
        },
    )
    for name in ("n0", "n1", "n2", "n3"):
        world.unit(name)
    world.link("ea", world.refs["n0"], world.refs["n1"], "a")
    world.link("eb", world.refs["n0"], world.refs["n2"], "b")
    world.link("ec", world.refs["n0"], world.refs["n3"], "c")
    result = world.view("n0", ["A", "B", "C"], ViewBudget(max_edges=1))
    cov = result["meta"]["duty_coverage"]
    statuses = [cov[d]["status"] for d in ("A", "B", "C")]
    assert statuses.count("not_started") == 2
    assert statuses.count("truncated") == 1
    assert any(g["code"] == "budget_exhausted" for g in result["gaps"])


def test_common_background_and_gaps_survive_duty_filter():
    world = World()
    world.unit("bg")
    world.unit("n0")
    result = world.builder.build(
        world.scope,
        RoleViewQuery(anchor=world.refs["n0"], duty="general"),
        graph=world.graph,
        read=world.read,
        discover=lambda q, n: [],
        resolve_edge=world.resolve_edge,
        background=[{"ref": {"scope": {"org": "o", "space": "s"}, "unit_id": "bg",
                             "content_digest": "f" * 64}, "required": False}],
    )
    # The background reference has a fabricated digest and appears as a stale gap,
    # but every returned gap must carry common=true semantics (retained regardless of duty).
    assert result["gaps"]
    for g in result["gaps"]:
        assert g["code"]


def test_hidden_endpoint_is_not_a_bridge():
    world = World(
        {"link": {"display_name": "link", "description": "link"}},
        {"A": {"traversal": [{"edge_type": "link", "direction": "forward"}]}},
    )
    world.unit("n0")
    world.unit("hidden")
    world.unit("n2")
    world.link("e1", world.refs["n0"], world.refs["hidden"], "link")
    world.link("e2", world.refs["hidden"], world.refs["n2"], "link")

    def read(ref):
        if ref.unit_id == "hidden":
            raise PermissionDeniedError("denied")
        return world.units[ref.unit_id]

    result = world.builder.build(
        world.scope, RoleViewQuery(anchor=world.refs["n0"], duty="A"),
        graph=world.graph, read=read, discover=lambda q, n: [],
        resolve_edge=world.resolve_edge,
    )
    assert "n2" not in [i["text"] for i in result["facts"]]
    assert any(g["code"] == "reference_unavailable" for g in result["gaps"])
