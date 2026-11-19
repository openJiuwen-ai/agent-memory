"""Query-side names cannot silently migrate between independent requirements."""

import pytest

from jiuwen_memory.common.graphmem_semantics import FEATURES_KEY, EntityLabel, GraphSemantics
from jiuwen_memory.common.type_def import ScoredMemoryUnit
from jiuwen_memory.retrieval.graphmem_query import GraphMemParsedQuery, QueryNeeds
from jiuwen_memory.retrieval.graphmem_relation_plan import (
    clause_specs,
    ground_query_entities,
    node_signals,
)
from tests.unit.construction.graphmem_fixtures import unit

pytestmark = pytest.mark.unit


def stored(*entities, text=""):
    value = unit("summary", text=text)
    value.system_metadata[FEATURES_KEY] = GraphSemantics(tuple(entities), ()).encode()
    return ScoredMemoryUnit(value, 1)


def test_entity_from_one_side_is_not_borrowed_by_an_unresolved_other_side():
    needs = QueryNeeds("collection", ("Lisbon",),
                       ("days spent in Lisbon", "days spent in Oslo"))
    assert [c["entities"] for c in clause_specs(needs)] == [["Lisbon"], []]


def test_subject_omitted_from_all_facets_remains_shared():
    needs = QueryNeeds("fact", ("Mira",), ("birthplace", "occupation"))
    assert [c["entities"] for c in clause_specs(needs)] == [["Mira"], ["Mira"]]


def test_explicit_sides_stay_separate_and_other_facets_use_only_shared_names():
    needs = QueryNeeds("comparison", ("Mira", "Lisbon", "Oslo"),
                       ("Lisbon visit", "Oslo visit", "occupation"))
    assert [c["entities"] for c in clause_specs(needs)] == [
        ["Lisbon"], ["Oslo"], ["Mira"]]


@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_possessive_keeps_object_scene_and_resolves_only_explicit_owner(apostrophe):
    name = f"Mira{apostrophe}s bicycle shop"
    needs = QueryNeeds("collection", (name,), ("offer",), analysis_status="used", model_calls=1)
    query = GraphMemParsedQuery(raw=f"What does {name} offer?", navigation_needs=needs)
    candidate = stored(EntityLabel("Mira", "person"),
                       text="Mira repairs bicycles at her bicycle shop.")
    resolved = ground_query_entities(query, [candidate])
    assert resolved.entities == ("Mira",)
    assert resolved.facets == ("bicycle shop offer",)
    assert resolved.model_calls == 1
    assert query.navigation_needs is needs and needs.entities == (name,)
    assert node_signals(candidate.unit, resolved)["clauses"] == ["C1"]
    unrelated = stored(EntityLabel("Mira", "person"), text="Mira enjoys mountain hiking.")
    assert node_signals(unrelated.unit, resolved)["clauses"] == []


def test_literal_named_business_is_not_replaced_with_its_owner():
    name = "Mira's Bicycle Shop"
    needs = QueryNeeds("fact", (name,), ("opening hours",))
    query = GraphMemParsedQuery(raw=f"When does {name} open?", navigation_needs=needs)
    candidate = stored(EntityLabel("Mira", "person"), EntityLabel(name, "organization"))
    assert ground_query_entities(query, [candidate]) is needs


def test_ambiguous_owner_identity_and_pronouns_do_not_resolve():
    for name, entities in [
        ("Mira's bicycle shop", [EntityLabel("Mira", "person", qualifier="north"),
                                EntityLabel("Mira", "person", qualifier="south")]),
        ("user's bicycle shop", [EntityLabel("Mira", "person", aliases=("user",))]),
    ]:
        needs = QueryNeeds("fact", (name,), ("opening hours",))
        query = GraphMemParsedQuery(raw=f"When does {name} open?", navigation_needs=needs)
        assert ground_query_entities(query, [stored(*entities)]) is needs


def test_existing_object_words_are_not_duplicated():
    needs = QueryNeeds("fact", ("Mira's bicycle shop",), ("bicycle shop hours",))
    query = GraphMemParsedQuery(raw="What are Mira's bicycle shop hours?", navigation_needs=needs)
    resolved = ground_query_entities(query, [stored(EntityLabel("Mira", "person"))])
    assert resolved.facets == needs.facets and resolved.entities == ("Mira",)
