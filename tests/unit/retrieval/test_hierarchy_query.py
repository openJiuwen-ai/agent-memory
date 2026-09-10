"""结构过滤下推、三条检索路径真源复核与父引用披露。"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta, timezone
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    HierarchyStatus,
    LifecycleState,
    ParsedQuery,
    RecallChannel,
    RetrievalPipeline,
    ScoredMemoryUnit,
)
from jiuwen_memory.common.type_def.entity import EntityRecord, EntityStoreFilters
from jiuwen_memory.common.type_def.hierarchy_query import HierarchyQuery
from jiuwen_memory.retrieval.discloser_impl.structured_discloser import StructuredDiscloser
from jiuwen_memory.retrieval.discloser_impl.truncating_discloser import TruncatingDiscloser
from jiuwen_memory.retrieval.retriever_impl.predicate_builder import build_hierarchy_filters
from jiuwen_memory.retrieval.types import DisclosureLevel, RetrievalQuery
from jiuwen_memory.storage.domain_store_impl.keyword_recaller import KeywordRecaller
from jiuwen_memory.storage.entity_store import EntityStore
from tests.unit.retrieval.hierarchy_query_fixtures import (
    QUERY_SCOPE,
    SPAN_END,
    SPAN_START,
    hierarchy_query,
    make_harness,
    tree_unit,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
@pytest.mark.parametrize("channel", [RecallChannel.KEYWORD, RecallChannel.VECTOR])
def test_structural_filter_applies_before_store_top_k(pipeline, channel) -> None:
    harness = make_harness(pipeline, recall_limit=1)
    ordinary = tree_unit("ordinary")
    ordinary.hierarchy = HierarchyRef()
    wrong_role = tree_unit("snapshot", HierarchyRole.SNAPSHOT)
    outside = tree_unit("outside")
    outside.hierarchy.span_start = SPAN_END + timedelta(hours=1)
    outside.hierarchy.span_end = SPAN_END + timedelta(hours=2)
    target = tree_unit("target")
    harness.add([ordinary, wrong_role, outside, target])

    result = harness.retriever.retrieve(
        QUERY_SCOPE, hierarchy_query(top_k=1, channels=[channel])
    )

    assert [item.unit_id for item in result.items] == ["target"]
    assert result.errors == []


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
@pytest.mark.parametrize("channel", [RecallChannel.KEYWORD, RecallChannel.VECTOR])
def test_stale_index_is_rechecked_against_structural_truth(pipeline, channel) -> None:
    harness = make_harness(pipeline)
    nodes = [tree_unit(label) for label in ("good", "role", "status", "span", "empty")]
    harness.add(nodes)
    nodes[1].hierarchy.role = HierarchyRole.SNAPSHOT
    nodes[2].hierarchy.status = HierarchyStatus.DISMISSED
    nodes[3].hierarchy.span_start = SPAN_END + timedelta(hours=1)
    nodes[3].hierarchy.span_end = SPAN_END + timedelta(hours=2)
    nodes[4].hierarchy = HierarchyRef()
    harness.domain.update(QUERY_SCOPE, nodes)

    result = harness.retriever.retrieve(QUERY_SCOPE, hierarchy_query(channels=[channel]))

    assert [item.unit_id for item in result.items] == ["good"]
    assert result.errors == []


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
def test_kind_only_query_keeps_roles_but_requires_valid_time_span(pipeline) -> None:
    harness = make_harness(pipeline)
    nodes = [tree_unit("parent"), tree_unit("leaf", HierarchyRole.SNAPSHOT)]
    nodes.extend([tree_unit("missing"), tree_unit("inverted"), tree_unit("dismissed")])
    harness.add(nodes)
    nodes[2].hierarchy.span_start = None
    nodes[2].hierarchy.span_end = None
    nodes[3].hierarchy.span_start = SPAN_END
    nodes[3].hierarchy.span_end = SPAN_START
    nodes[4].hierarchy.status = HierarchyStatus.DISMISSED
    harness.domain.update(QUERY_SCOPE, nodes)

    result = harness.retriever.retrieve(
        QUERY_SCOPE, hierarchy_query(hierarchy_role=None, span_start=None, span_end=None)
    )

    assert {item.unit_id for item in result.items} == {"parent", "leaf"}


@pytest.mark.parametrize("channel", [RecallChannel.KEYWORD, RecallChannel.VECTOR])
def test_user_or_not_cannot_weaken_structure_or_permission_filters(channel) -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    ordinary = tree_unit("ordinary")
    ordinary.hierarchy = HierarchyRef()
    forbidden = tree_unit("forbidden")
    permitted = tree_unit("permitted")
    for node in (ordinary, permitted):
        node.system_metadata["author"] = "alice"
    forbidden.system_metadata["author"] = "bob"
    permitted.user_metadata["project"] = "personal"
    harness.add([ordinary, forbidden, permitted])
    user_or = FilterGroup(FilterLogic.OR, [
        FilterClause("user_metadata.project", FilterOp.EQ, "missing"),
        FilterGroup(FilterLogic.NOT, [FilterClause("user_metadata.blocked", FilterOp.EQ, True)]),
    ])
    filters = FilterGroup(FilterLogic.AND, [
        user_or, FilterClause("system_metadata.author", FilterOp.EQ, "alice"),
    ])

    result = harness.retriever.retrieve(
        QUERY_SCOPE, hierarchy_query(filters=filters, channels=[channel])
    )

    assert [item.unit_id for item in result.items] == ["permitted"]


@pytest.mark.parametrize("channel", [RecallChannel.KEYWORD, RecallChannel.VECTOR])
def test_same_named_user_metadata_cannot_forge_structure(channel) -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    ordinary = tree_unit("ordinary")
    ordinary.hierarchy = HierarchyRef()
    ordinary.user_metadata = {"hierarchy_kind": "time", "hierarchy_role": "time_span"}
    structured = tree_unit("structured")
    structured.user_metadata = {"hierarchy_kind": "business", "hierarchy_role": "owner"}
    structured.system_metadata = {"hierarchy_kind": "control"}
    harness.add([ordinary, structured])

    result = harness.retriever.retrieve(QUERY_SCOPE, hierarchy_query(channels=[channel]))
    explicit_user = harness.retriever.retrieve(QUERY_SCOPE, RetrievalQuery(
        text="recall", channels=[channel],
        filters=FilterClause("user_metadata.hierarchy_kind", FilterOp.EQ, "time"),
    ))
    combined = harness.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        channels=[channel],
        filters=FilterClause("user_metadata.hierarchy_kind", FilterOp.EQ, "business"),
    ))

    assert [item.unit_id for item in result.items] == ["structured"]
    assert [item.unit_id for item in explicit_user.items] == ["ordinary"]
    assert [item.unit_id for item in combined.items] == ["structured"]


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
def test_structure_window_is_closed_and_microsecond_truth_is_preserved(pipeline) -> None:
    harness = make_harness(pipeline)
    boundary = tree_unit("boundary")
    boundary.hierarchy.span_end = SPAN_START
    outside = deepcopy(boundary)
    outside.id = "microsecond-outside"
    outside.hierarchy.span_end = SPAN_START - timedelta(microseconds=1)
    outside.hierarchy.span_start = SPAN_START - timedelta(seconds=1)
    harness.add([outside, boundary])

    result = harness.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        span_start=SPAN_START.replace(tzinfo=None),
        span_end=SPAN_START.astimezone(timezone(timedelta(hours=8))),
    ))

    assert [item.unit_id for item in result.items] == ["boundary"]


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
def test_structure_event_and_valid_time_are_independent(pipeline) -> None:
    harness = make_harness(pipeline)
    chosen = tree_unit("chosen")
    event_outside = tree_unit("event-outside")
    not_yet_valid = tree_unit("not-yet-valid")
    for node in (chosen, event_outside, not_yet_valid):
        node.temporal.t_valid = SPAN_START - timedelta(days=1)
        node.temporal.t_event = SPAN_START + timedelta(days=20)
    event_outside.temporal.t_event += timedelta(days=5)
    not_yet_valid.temporal.t_valid = SPAN_START + timedelta(days=1)
    harness.add([chosen, event_outside, not_yet_valid])
    harness.parser.event_window = (
        SPAN_START + timedelta(days=20), SPAN_START + timedelta(days=21),
    )

    result = harness.retriever.retrieve(
        QUERY_SCOPE, hierarchy_query(as_of=SPAN_START)
    )

    assert [item.unit_id for item in result.items] == ["chosen"]


def test_ordinary_query_does_not_implicitly_enable_hierarchy_filtering() -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    ordinary = tree_unit("ordinary")
    ordinary.hierarchy = HierarchyRef()
    dismissed = tree_unit("dismissed")
    dismissed.hierarchy.status = HierarchyStatus.DISMISSED
    harness.add([ordinary, dismissed])

    result = harness.retriever.retrieve(QUERY_SCOPE, RetrievalQuery(text="recall"))

    assert {item.unit_id for item in result.items} == {"ordinary", "dismissed"}
    assert build_hierarchy_filters(HierarchyQuery()) == []


def test_archived_lifecycle_does_not_override_dismissed_structure() -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    archived = tree_unit("archived")
    dismissed = tree_unit("dismissed")
    archived.lifecycle = LifecycleState.ARCHIVED
    dismissed.lifecycle = LifecycleState.ARCHIVED
    dismissed.hierarchy.status = HierarchyStatus.DISMISSED
    harness.add([archived, dismissed])

    result = harness.retriever.retrieve(
        QUERY_SCOPE, hierarchy_query(include_archived=True)
    )

    assert [item.unit_id for item in result.items] == ["archived"]


def test_third_party_ranked_result_still_gets_truth_recheck() -> None:
    harness = make_harness(RetrievalPipeline.RETRIEVE, unchecked_ranked=True)
    ordinary = tree_unit("ordinary")
    ordinary.hierarchy = HierarchyRef()
    harness.add([ordinary, tree_unit("structured")])

    result = harness.retriever.retrieve(QUERY_SCOPE, hierarchy_query())

    assert [item.unit_id for item in result.items] == ["structured"]


@pytest.mark.parametrize("discloser_type", [TruncatingDiscloser, StructuredDiscloser])
@pytest.mark.parametrize("level", list(DisclosureLevel))
def test_parent_id_comes_from_true_structure_for_all_disclosure_levels(
    discloser_type, level,
) -> None:
    """所有披露粒度的父引用均取自真实结构，不取同名元数据。"""
    node = tree_unit("leaf", HierarchyRole.SNAPSHOT)
    node.hierarchy.parent_id = "actual-parent"
    node.user_metadata["parent_id"] = "user-parent"
    node.system_metadata["parent_id"] = "stale-parent"

    items = discloser_type().disclose(
        ParsedQuery(raw="recall"), [ScoredMemoryUnit(node, 1.0)], {node.id: node}, level,
        max_tokens=100,
    )

    assert items[0].parent_id == "actual-parent"
    assert items[0].user_metadata["parent_id"] == "user-parent"
    assert items[0].system_metadata["parent_id"] == "stale-parent"


def test_non_time_kind_can_be_queried_without_span() -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    topic = tree_unit("topic")
    topic.hierarchy = HierarchyRef(kind=HierarchyKind.TOPIC, role=HierarchyRole.NODE)
    harness.add([tree_unit("time"), topic])

    result = harness.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        hierarchy_kind=HierarchyKind.TOPIC, hierarchy_role=HierarchyRole.NODE,
        span_start=None, span_end=None,
    ))

    assert [item.unit_id for item in result.items] == ["topic"]


def test_entity_extension_rechecks_structure_without_matching_index_document() -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    seed = tree_unit("seed")
    seed.entities = ["alice"]
    harness.add([seed])
    eligible = tree_unit("eligible")
    ordinary = tree_unit("ordinary")
    ordinary.hierarchy = HierarchyRef()
    dismissed = tree_unit("dismissed")
    dismissed.hierarchy.status = HierarchyStatus.DISMISSED
    harness.domain.add(QUERY_SCOPE, [eligible, ordinary, dismissed])
    entity_store = Mock(spec=EntityStore)
    entity_store.find_by_entity_text_hash.return_value = [EntityRecord(
        id="alice", space_id="space", entity_text="alice", entity_type="person",
        linked_memory_ids=("eligible", "ordinary", "dismissed"),
        filters=EntityStoreFilters(actor_id="alice"),
    )]
    query = ParsedQuery(
        raw="recall", hierarchy_kind=HierarchyKind.TIME,
        hierarchy_role=HierarchyRole.TIME_SPAN, span_start=SPAN_START, span_end=SPAN_END,
    )

    candidates = KeywordRecaller(harness.manager, entity_store=entity_store).recall(
        QUERY_SCOPE, query, 10
    )

    assert {candidate.unit_id for candidate in candidates} == {"seed", "eligible"}
    entity_store.find_by_entity_text_hash.assert_called_once()


def test_empty_text_does_not_turn_hierarchy_search_into_tree_enumeration() -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    harness.add([tree_unit("node")])

    result = harness.retriever.retrieve(QUERY_SCOPE, hierarchy_query(text=" "))

    assert result.items == []


def test_mutated_internal_query_is_validated_again_before_retrieval() -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    query = hierarchy_query()
    query.hierarchy_kind = "time"

    with pytest.raises(ValidationError, match="HierarchyKind"):
        harness.retriever.retrieve(QUERY_SCOPE, query)


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
@pytest.mark.parametrize("channel", [RecallChannel.KEYWORD, RecallChannel.VECTOR])
def test_ordinary_as_of_keeps_unbounded_start_and_half_open_validity(pipeline, channel) -> None:
    harness = make_harness(pipeline)
    labels = ("unbounded", "before", "at", "after", "expired-at", "forgotten")
    nodes = [tree_unit(label) for label in labels]
    for node in nodes:
        node.hierarchy = HierarchyRef()
    nodes[1].temporal.t_valid = SPAN_START - timedelta(days=1)
    nodes[2].temporal.t_valid = SPAN_START
    nodes[3].temporal.t_valid = SPAN_START + timedelta(days=1)
    nodes[4].temporal.t_invalid = SPAN_START
    nodes[5].lifecycle = LifecycleState.FORGOTTEN
    harness.add(nodes)

    result = harness.retriever.retrieve(QUERY_SCOPE, RetrievalQuery(
        text="recall", as_of=SPAN_START, channels=[channel],
    ))

    assert {item.unit_id for item in result.items} == {"unbounded", "before", "at"}
    assert result.errors == []


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
@pytest.mark.parametrize("channel", [RecallChannel.KEYWORD, RecallChannel.VECTOR])
def test_ordinary_id_and_unit_id_filters_are_equivalent(pipeline, channel) -> None:
    harness = make_harness(pipeline, recall_limit=1)
    nodes = [tree_unit(label) for label in ("other", "target")]
    for node in nodes:
        node.hierarchy = HierarchyRef()
    harness.add(nodes)
    results = []
    for field_name in ("id", "unit_id"):
        results.append(harness.retriever.retrieve(QUERY_SCOPE, RetrievalQuery(
            text="recall", top_k=1, channels=[channel],
            filters=FilterClause(field_name, FilterOp.EQ, "target"),
        )))

    assert [item.unit_id for item in results[0].items] == ["target"]
    assert [item.unit_id for item in results[1].items] == ["target"]


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
@pytest.mark.parametrize("channel", [RecallChannel.KEYWORD, RecallChannel.VECTOR])
def test_ordinary_message_time_filter_uses_indexed_epoch_milliseconds(pipeline, channel) -> None:
    harness = make_harness(pipeline, recall_limit=1)
    nodes = [tree_unit(label) for label in ("unknown", "outside", "target")]
    for node in nodes:
        node.hierarchy = HierarchyRef()
    nodes[1].temporal.t_message = SPAN_START - timedelta(days=1)
    nodes[2].temporal.t_message = SPAN_START
    harness.add(nodes)
    timestamp_ms = int(SPAN_START.timestamp() * 1000)
    condition = FilterGroup(FilterLogic.AND, [
        FilterClause("t_message", FilterOp.GTE, timestamp_ms),
        FilterClause("t_message", FilterOp.LTE, timestamp_ms),
    ])

    result = harness.retriever.retrieve(QUERY_SCOPE, RetrievalQuery(
        text="recall", top_k=1, channels=[channel], filters=condition,
    ))

    assert [item.unit_id for item in result.items] == ["target"]
    assert result.errors == []


def test_message_time_projection_is_shared_and_absent_for_none() -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    unknown = tree_unit("unknown")
    known = tree_unit("known")
    known.temporal.t_message = SPAN_START
    harness.add([unknown, known])

    documents = harness.manager.fulltext().get(QUERY_SCOPE, ["unknown", "known"])
    vectors = harness.manager.vector().get(QUERY_SCOPE, ["unknown-0", "known-0"])

    assert "t_message" not in documents[0].metadata
    assert "t_message" not in vectors[0].metadata
    assert documents[1].metadata["t_message"] == int(SPAN_START.timestamp() * 1000)
    assert vectors[1].metadata["t_message"] == documents[1].metadata["t_message"]
