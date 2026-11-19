"""Event-reference repair improves representation without replacing covered events."""

from copy import deepcopy

import pytest

from jiuwen_memory.common.type_def import ScoredMemoryUnit, Segment
from jiuwen_memory.retrieval.evidence_packing import EvidencePacker
from jiuwen_memory.retrieval.expander import node_key
from jiuwen_memory.retrieval.graphmem_evidence_graph import graph_order, pack_graph, render_graph
from jiuwen_memory.retrieval.graphmem_query import GraphMemParsedQuery, QueryNeeds
from jiuwen_memory.retrieval.graphmem_relation_plan import compile_program
from jiuwen_memory.retrieval.graphmem_relational_navigation import RelationalEvidencePlan
from tests.unit.construction.graphmem_fixtures import tree

pytestmark = pytest.mark.unit


def fixture(texts, references):
    parent, raw = tree("parent", "session")
    children = []
    for i, text in enumerate(texts):
        child = deepcopy(raw)
        child.id = f"source-{i}"
        child.segments = [Segment(content=text)]
        children.append(ScoredMemoryUnit(child, 1))
    parent.hierarchy.child_ids = [c.unit.id for c in children]
    parent.hierarchy.child_scopes = [deepcopy(c.unit.scope) for c in children]
    query = GraphMemParsedQuery(raw="Which events did Alice attend?", navigation_needs=
        QueryNeeds("collection", ("Alice",), ("events attended",)))
    plan = RelationalEvidencePlan(candidates=[ScoredMemoryUnit(parent, 1), *children],
        program=compile_program(query).describe(),
        reasons={node_key(parent): {"selected_by": "initial_vector_keyword_match"}})
    for i, (child, events) in enumerate(zip(children, references)):
        plan.links.append({"source": node_key(parent), "target": node_key(child.unit),
            "kind": "contains", "reason": {"verified_child_link": True,
            "event_references": [{"event_id": event, "source_position": i, "clauses": ["C1"]}
                                 for event in events]}})
    return plan, children, EvidencePacker(len, "chars", mode="sources_first",
                                          header_style="compact", max_summaries=0)


def budget_for(items, plan):
    return len(render_graph(graph_order(items, plan), plan)[0])


def test_missing_matched_event_source_can_replace_unrelated_background():
    plan, children, packer = fixture(
        ["Alice attended A.", "Background " * 200, "Alice attended B."], [["A"], [], ["B"]])
    budget = budget_for(children[:2], plan)
    selected, bundle = pack_graph(plan, packer, max_tokens=budget, top_k=32, summary_tokens=640)
    assert children[0] in selected and children[2] in selected and children[1] not in selected
    coverage = bundle.navigation["event_reference_coverage"]
    assert coverage["before"] == 1 and coverage["represented"] == 2 and coverage["refined"]
    assert bundle.token_count == len(bundle.rendered_context) <= budget


def test_redundant_reference_does_not_displace_existing_context():
    plan, children, packer = fixture(
        ["Alice attended A.", "Background " * 200, "Alice repeated A."], [["A"], [], ["A"]])
    selected, bundle = pack_graph(plan, packer, max_tokens=budget_for(children[:2], plan),
                                  top_k=32, summary_tokens=640)
    assert selected == children[:2]
    assert not bundle.navigation["event_reference_coverage"]["refined"]
    assert bundle.navigation["event_reference_coverage"]["missing"] == 0


def test_repair_may_not_trade_an_existing_event_for_another_event():
    plan, children, packer = fixture(["d" * 1000, "b" * 1800, "a" * 1000],
                                     [[], ["B"], ["A"]])
    baseline = [children[0], children[2]]
    selected, bundle = pack_graph(plan, packer, max_tokens=budget_for(baseline, plan),
                                  top_k=32, summary_tokens=640)
    assert selected == baseline
    assert not bundle.navigation["event_reference_coverage"]["refined"]
    assert bundle.navigation["event_reference_coverage"]["missing"] == 1


def test_unlinked_event_reason_alone_cannot_create_a_representation_obligation():
    plan, children, packer = fixture(["a", "Background " * 200, "b"], [["A"], [], []])
    plan.reasons[node_key(children[2].unit)] = {"parent_event_references": [{"event_id": "B"}]}
    selected, bundle = pack_graph(plan, packer, max_tokens=budget_for(children[:2], plan),
                                  top_k=32, summary_tokens=640)
    assert selected == children[:2]
    assert bundle.navigation["event_reference_coverage"]["available"] == 1
