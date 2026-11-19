"""Query algebra words do not become required evidence-scene content."""

import pytest

from jiuwen_memory.retrieval.graphmem_query import QueryNeeds
from jiuwen_memory.retrieval.graphmem_relation_plan import node_signals, scene_terms
from tests.unit.construction.graphmem_fixtures import tree

pytestmark = pytest.mark.unit


def test_collection_evidence_need_not_repeat_counting_operator():
    needs = QueryNeeds("collection", ("Maya",), ("total number of completed repairs",))
    parent, _ = tree("p", "s", text="Maya completed six repairs.", raw="Maya finished six repairs.")
    assert scene_terms(needs) == [{"completed", "repair"}]
    assert node_signals(parent, needs)["clauses"] == ["C1"]
    assert needs.facets == ("total number of completed repairs",)


def test_literal_number_remains_a_fact_query_target():
    needs = QueryNeeds("fact", ("Maya",), ("number printed on ticket",))
    assert "number" in scene_terms(needs)[0]


def test_collection_operator_alone_is_not_scene_support():
    needs = QueryNeeds("collection", ("Maya",), ("total number",))
    parent, _ = tree("p", "s", text="Maya's number is confidential.", raw="Maya said hello.")
    assert scene_terms(needs) == [set()]
    assert not node_signals(parent, needs)["clauses"]


def test_count_operator_removal_does_not_weaken_entity_binding():
    needs = QueryNeeds("collection", ("Maya",), ("total repairs Maya will be doing",))
    parent, _ = tree("p", "s", text="Theo completed many repairs.", raw="Theo completed repairs.")
    assert scene_terms(needs) == [{"repair", "will", "be", "doing"}]
    assert not node_signals(parent, needs)["clauses"]


def test_noncounting_match_keeps_existing_lexical_evidence():
    needs = QueryNeeds("fact", ("Maya",), ("relaxing experience at campsite",))
    parent, _ = tree("p", "s", text="Maya enjoyed relaxation at the campsite.",
                     raw="Maya relaxed at the campsite.")
    assert node_signals(parent, needs)["clauses"] == ["C1"]


def test_negation_and_distinguishing_scene_terms_are_preserved():
    needs = QueryNeeds("collection", ("Maya",), ("total repairs not completed",))
    assert scene_terms(needs) == [{"repair", "not", "completed"}]
