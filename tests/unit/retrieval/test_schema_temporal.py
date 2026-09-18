"""Schema TemporalEntity assembly, selection and fallback contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    ParsedQuery,
    RecallBatch,
    RecallChannel,
    RecallResult,
    RetrievalPipeline,
    Scope,
    ScoredCandidate,
    ScoredMemoryUnit,
    Segment,
    Temporal,
    matches_memory_unit,
)
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.discloser import Discloser
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.query_parser import QueryParser
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.schema_temporal import (
    EventTimePrecision,
    SchemaTemporalMode,
    SchemaTemporalQuery,
    SchemaTemporalReader,
    TemporalEntityAssembler,
    TemporalEntitySelector,
)
from jiuwen_memory.retrieval.schema_temporal.pipeline import SchemaTemporalPipelineExtension
from jiuwen_memory.retrieval.schema_temporal.query import resolve_temporal_query
from jiuwen_memory.retrieval.schema_temporal.recall import SchemaDualPathRecaller
from jiuwen_memory.retrieval.types import DisclosureLevel, RetrievalQuery, RetrievedItem
from jiuwen_memory.storage.types import MemoryListResult, VectorRecord
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="org", user="alice")


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _property(
    unit_id: str,
    *,
    entity_key: str = "entity-alice",
    property_name: str = "occupation",
    value: str | None = None,
    event_start: str | None = None,
    event_end: str | None = None,
    precision: str | None = None,
    valid_from: str = "2020-01-01T00:00:00",
    valid_to: str | None = None,
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    provenance: list[str] | None = None,
) -> MemoryUnit:
    metadata = {
        "extraction_mode": "schema",
        "schema_entity_key": entity_key,
        "schema_entity_name": entity_key.removeprefix("entity-"),
        "schema_entity_type": "person",
        "schema_property_name": property_name,
        "schema_property_operation": "set",
    }
    if precision is not None:
        metadata["schema_event_precision"] = precision
    if event_start is not None:
        metadata["schema_event_start"] = _dt(event_start).isoformat()
    if event_end is not None:
        metadata["schema_event_end"] = _dt(event_end).isoformat()
    return MemoryUnit(
        id=unit_id,
        scope=_SCOPE,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content=value or unit_id)],
        temporal=Temporal(
            t_event=(
                _dt(event_start)
                if event_start is not None and precision in {"day", "datetime"}
                else None
            ),
            t_ingest=_dt(valid_from),
            t_valid=_dt(valid_from),
            t_invalid=_dt(valid_to) if valid_to else None,
        ),
        provenance=list(provenance or []),
        system_metadata=metadata,
        lifecycle=lifecycle,
    )


def _source(unit_id: str, entity_key: str) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=_SCOPE,
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content=f"source evidence for {entity_key}")],
        temporal=Temporal(t_valid=_dt("2020-01-01T00:00:00")),
        system_metadata={
            "schema_source_evidence": True,
            "schema_entity_keys": [entity_key],
        },
    )


class _FakeDomain:
    """Minimal DomainStore-shaped fake with observable list and point reads."""

    def __init__(
        self,
        units: list[MemoryUnit],
        *,
        recalled_ids: list[str] | None = None,
    ) -> None:
        self.units = {unit.id: unit for unit in units}
        self.recalled_ids = list(recalled_ids or [])
        self.get_calls: list[list[str]] = []
        self.list_calls = 0

    @staticmethod
    def preferred_retrieval_pipeline() -> RetrievalPipeline:
        return RetrievalPipeline.RECALL_AND_GET_RANK

    def get(self, scope: Scope, unit_ids: list[str]) -> list[MemoryUnit]:
        self.get_calls.append(list(unit_ids))
        return [
            self.units[unit_id]
            for unit_id in unit_ids
            if unit_id in self.units and self.units[unit_id].scope == scope
        ]

    def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        filters=None,
        **_options,
    ) -> MemoryListResult:
        self.list_calls += 1
        matched = [
            unit
            for unit in self.units.values()
            if unit.scope == scope and matches_memory_unit(unit, filters)
        ]
        return MemoryListResult(items=matched[offset : offset + limit], count=len(matched))

    def recall_and_get(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
    ) -> RecallResult[ScoredMemoryUnit]:
        del channels
        candidates: list[ScoredMemoryUnit] = []
        for rank, unit_id in enumerate(self.recalled_ids[:recall_limit], start=1):
            unit = self.units.get(unit_id)
            if unit is None or unit.scope != scope:
                continue
            if not matches_memory_unit(unit, query.scalar_filters):
                continue
            candidates.append(
                ScoredMemoryUnit(
                    unit=unit,
                    score=1.0 / rank,
                    channel=RecallChannel.KEYWORD,
                )
            )
        return RecallResult(batches=[RecallBatch(RecallChannel.KEYWORD, "fake", candidates)])


@dataclass(frozen=True)
class _Lookup:
    indexed: bool
    unit_ids: list[str]


class _StaticParser(QueryParser):
    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.QUERY_PARSER

    def health(self) -> None:
        return None

    def parse(self, query: RetrievalQuery) -> ParsedQuery:
        return ParsedQuery(
            raw=query.text,
            rewritten=query.text,
            tokens=query.text.split(),
            keywords=query.text.split(),
        )


class _PlainDiscloser(Discloser):
    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.DISCLOSER

    def health(self) -> None:
        return None

    def disclose(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
        units: dict[str, MemoryUnit],
        level: DisclosureLevel,
        max_tokens: int | None = None,
    ) -> list[RetrievedItem]:
        del query, max_tokens
        return [
            RetrievedItem(
                unit_id=candidate.unit_id,
                score=candidate.score,
                content=units[candidate.unit_id].content,
                level=level,
            )
            for candidate in candidates
        ]


def _parsed(text: str, *, entity_limit: int = 20) -> ParsedQuery:
    return ParsedQuery(
        raw=text,
        rewritten=text,
        tokens=text.split(),
        keywords=text.split(),
        extensions={
            "schema_temporal": {
                "mode": "history",
                "entity_limit": entity_limit,
            }
        },
    )


def test_entity_multivector_hits_are_deduplicated_to_one_entity() -> None:
    entity_vectors = InMemoryVectorStore()
    metadata = {
        "record_kind": "schema_entity",
        "schema_entity_id": "entity-alice",
        "entity_vector_owner_id": "entity-alice",
    }
    entity_vectors.insert(
        _SCOPE,
        [
            VectorRecord("entity-alice", [1.0, 0.0], metadata),
            VectorRecord("entity-alice#sf0", [1.0, 0.0], metadata),
        ],
    )
    recaller = SchemaDualPathRecaller(
        _FakeDomain([]),
        entity_vector_store=entity_vectors,
    )

    candidates = recaller.recall_entities(
        _SCOPE,
        ParsedQuery(raw="Alice", rewritten="Alice", vector=[1.0, 0.0]),
        entity_limit=20,
    )

    assert [candidate.entity_id for candidate in candidates] == ["entity-alice"]
    assert candidates[0].channels == {"entity_vector"}


def test_temporal_entity_supports_latest_snapshot_range_and_history() -> None:
    old = _property(
        "job-2020",
        value="Alice was a designer",
        event_start="2020-01-01T00:00:00",
        event_end="2021-01-01T00:00:00",
        precision="year",
        lifecycle=LifecycleState.SUPERSEDED,
        valid_to="2022-01-01T00:00:00",
    )
    current = _property(
        "job-2021",
        value="Alice is an engineer",
        event_start="2021-01-01T00:00:00",
        event_end="2022-01-01T00:00:00",
        precision="year",
        valid_from="2022-01-01T00:00:00",
    )
    undated = _property(
        "hobby-undated",
        property_name="hobby",
        value="Alice likes chess",
    )
    entity = TemporalEntityAssembler().assemble("entity-alice", [old, current, undated])

    latest = entity.select(SchemaTemporalQuery(mode=SchemaTemporalMode.LATEST))
    history = entity.select(
        SchemaTemporalQuery(mode=SchemaTemporalMode.HISTORY, include_archived=True)
    )
    snapshot = entity.select(
        SchemaTemporalQuery(
            mode=SchemaTemporalMode.SNAPSHOT,
            event_at=_dt("2020-06-01T00:00:00"),
            include_archived=True,
        )
    )
    ranged = entity.select(
        SchemaTemporalQuery(
            mode=SchemaTemporalMode.RANGE,
            event_from=_dt("2020-06-01T00:00:00"),
            event_to=_dt("2021-06-01T00:00:00"),
            include_archived=True,
        )
    )

    assert latest["occupation"].unit_id == current.id
    assert latest["hobby"].unit_id == undated.id
    assert [entry.unit_id for entry in history["occupation"]] == [old.id, current.id]
    assert snapshot["occupation"].unit_id == old.id
    assert [entry.unit_id for entry in ranged["occupation"]] == [old.id, current.id]
    assert "hobby" not in ranged, "range excludes undated facts by default"


def test_legacy_datetime_without_end_is_an_instant_for_timepoint_filter() -> None:
    instant = _property(
        "instant",
        value="Alice arrived",
        event_start="2024-03-01T12:00:00",
        precision="datetime",
    )
    entity = TemporalEntityAssembler().assemble("entity-alice", [instant])

    at_event = entity.filter_by_timepoints([_dt("2024-03-01T12:00:00")])
    much_later = entity.filter_by_timepoints([_dt("2025-03-01T12:00:00")])

    assert at_event.properties["occupation"][0].unit_id == instant.id
    assert much_later.properties == {}


def test_query_mapping_parses_string_booleans_without_truthiness_leak() -> None:
    query = SchemaTemporalQuery.from_mapping(
        {
            "mode": "history",
            "include_undated": "false",
            "include_archived": "off",
        }
    )

    assert query.include_undated is False
    assert query.include_archived is False


def test_auto_query_detects_english_month_as_half_open_range() -> None:
    plan = resolve_temporal_query(
        None,
        ParsedQuery(raw="What happened in August 2023?"),
        auto_enabled=True,
    )

    assert plan.enabled
    assert plan.query.mode is SchemaTemporalMode.RANGE
    assert plan.query.event_precision is EventTimePrecision.MONTH
    assert plan.query.event_from == _dt("2023-08-01T00:00:00")
    assert plan.query.event_to == _dt("2023-09-01T00:00:00")


def test_typed_query_inherits_top_level_knowledge_controls() -> None:
    as_of = _dt("2023-08-15T00:00:00")
    plan = resolve_temporal_query(
        SchemaTemporalQuery(mode=SchemaTemporalMode.HISTORY),
        ParsedQuery(
            raw="What did Alice do?",
            as_of=as_of,
            include_archived=True,
        ),
        auto_enabled=False,
    )

    assert plan.query.knowledge_as_of == as_of
    assert plan.query.include_archived is True


def test_knowledge_as_of_and_include_archived_are_independent_visibility_axes() -> None:
    old = _property(
        "old",
        lifecycle=LifecycleState.SUPERSEDED,
        valid_to="2022-01-01T00:00:00",
    )
    current = _property("current", valid_from="2022-01-01T00:00:00")
    archived = _property(
        "archived",
        property_name="hobby",
        lifecycle=LifecycleState.ARCHIVED,
    )
    entity = TemporalEntityAssembler().assemble(
        "entity-alice",
        [old, current, archived],
    )

    known_2021 = entity.history(knowledge_as_of=_dt("2021-06-01T00:00:00"))
    known_2023 = entity.history(knowledge_as_of=_dt("2023-06-01T00:00:00"))
    default_now = entity.history()
    opened_now = entity.history(include_archived=True)

    assert [entry.unit_id for entry in known_2021["occupation"]] == [old.id]
    assert [entry.unit_id for entry in known_2023["occupation"]] == [current.id]
    assert "hobby" not in default_now
    assert [entry.unit_id for entry in opened_now["hobby"]] == [archived.id]


def test_property_recall_includes_superseded_history_only_when_requested() -> None:
    superseded = _property(
        "old-job",
        value="Alice was a designer",
        lifecycle=LifecycleState.SUPERSEDED,
        valid_to="2022-01-01T00:00:00",
    )
    domain = _FakeDomain([superseded], recalled_ids=[superseded.id])
    selector = TemporalEntitySelector(domain, source_fallback_enabled=False)

    current, _units = selector.select(
        _SCOPE,
        _parsed("Alice job"),
        [],
    )
    with_history, _units = selector.select(
        _SCOPE,
        ParsedQuery(
            raw="Alice job",
            rewritten="Alice job",
            tokens=["Alice", "job"],
            keywords=["Alice", "job"],
            extensions={
                "schema_temporal": {
                    "mode": "history",
                    "include_archived": True,
                }
            },
        ),
        [],
    )

    assert current == []
    assert [candidate.unit_id for candidate in with_history] == [superseded.id]


def test_reader_distinguishes_unindexed_fallback_from_indexed_point_reads() -> None:
    wanted = _property("wanted")
    other = _property("other", entity_key="entity-bob")

    fallback_domain = _FakeDomain([wanted, other])
    fallback_reader = SchemaTemporalReader(
        fallback_domain,
        property_id_lookup=lambda _scope, _entity: _Lookup(False, []),
    )
    fallback = fallback_reader.read(
        _SCOPE,
        ["entity-alice"],
        SchemaTemporalQuery(mode=SchemaTemporalMode.HISTORY),
    )

    indexed_domain = _FakeDomain([wanted, other])
    indexed_reader = SchemaTemporalReader(
        indexed_domain,
        property_id_lookup=lambda _scope, _entity: _Lookup(True, [wanted.id]),
    )
    indexed = indexed_reader.read(
        _SCOPE,
        ["entity-alice"],
        SchemaTemporalQuery(mode=SchemaTemporalMode.HISTORY),
    )

    assert fallback_domain.list_calls == 1
    assert fallback_domain.get_calls == []
    assert fallback.entities[0].properties["occupation"][0].unit_id == wanted.id
    assert indexed_domain.list_calls == 0
    assert indexed_domain.get_calls == [[wanted.id]]
    assert indexed.entities[0].properties["occupation"][0].unit_id == wanted.id


def test_indexed_reader_keeps_newest_units_when_entity_limit_truncates_history() -> None:
    old = _property("old", valid_from="2020-01-01T00:00:00")
    recent = _property("recent", valid_from="2024-01-01T00:00:00")
    domain = _FakeDomain([old, recent])
    reader = SchemaTemporalReader(
        domain,
        property_id_lookup=lambda _scope, _entity: _Lookup(
            True,
            [old.id, recent.id],
        ),
    )

    result = reader.read(
        _SCOPE,
        ["entity-alice"],
        SchemaTemporalQuery(
            mode=SchemaTemporalMode.LATEST,
            per_entity_limit=1,
        ),
    )

    assert result.entities[0].properties["occupation"][0].unit_id == recent.id
    assert result.entities[0].truncated is True


def test_direct_property_hit_survives_entity_limit_and_returns_real_unit() -> None:
    source = _source("source-first", "entity-first")
    first = _property("first", entity_key="entity-first", value="unrelated fact")
    direct = _property(
        "direct",
        entity_key="entity-second",
        value="The Paris trip happened in 2023",
    )
    domain = _FakeDomain([source, first, direct], recalled_ids=[direct.id])
    selector = TemporalEntitySelector(
        domain,
        property_top_n=10,
        source_fallback_enabled=False,
    )
    ordinary = [
        ScoredMemoryUnit(source, 1.0, RecallChannel.KEYWORD),
        ScoredMemoryUnit(first, 0.9, RecallChannel.VECTOR),
    ]

    selected, units = selector.select(
        _SCOPE,
        _parsed("Paris", entity_limit=1),
        ordinary,
    )

    assert direct.id in [candidate.unit_id for candidate in selected]
    assert units[direct.id] is direct
    assert all(candidate.unit is domain.units[candidate.unit_id] for candidate in selected)
    assert selector.last_result is not None
    assert direct.id in {
        unit_id for ids in selector.last_result.direct_property_unit_ids.values() for unit_id in ids
    }


def test_direct_property_hit_expands_three_neighbors_on_each_side() -> None:
    properties = [
        _property(
            f"job-{index}",
            value=f"Alice job history {index}",
            event_start=f"202{index}-01-01T00:00:00",
            event_end=f"202{index + 1}-01-01T00:00:00",
            precision="year",
        )
        for index in range(7)
    ]
    direct = properties[3]
    domain = _FakeDomain(properties, recalled_ids=[direct.id])
    selector = TemporalEntitySelector(
        domain,
        property_top_n=1,
        max_properties_per_entity=1,
        property_extension_step=3,
        source_fallback_enabled=False,
    )

    selected, _units = selector.select(
        _SCOPE,
        _parsed("history"),
        [ScoredMemoryUnit(properties[0], 1.0, RecallChannel.KEYWORD)],
        limit=10,
    )

    assert {candidate.unit_id for candidate in selected} == {
        property_unit.id for property_unit in properties
    }
    assert selector.last_result is not None
    assert selector.last_result.shrink_diagnostics is not None
    assert selector.last_result.shrink_diagnostics.timeline_neighbors_added == 6


def test_range_removes_timeline_neighbors_outside_the_requested_window() -> None:
    properties = [
        _property(
            f"job-{year}",
            value=f"Alice job in {year}",
            event_start=f"{year}-01-01T00:00:00",
            event_end=f"{year + 1}-01-01T00:00:00",
            precision="year",
        )
        for year in (2021, 2022, 2023)
    ]
    direct = properties[1]
    domain = _FakeDomain(properties, recalled_ids=[direct.id])
    selector = TemporalEntitySelector(
        domain,
        property_top_n=1,
        property_extension_step=1,
        source_fallback_enabled=False,
    )
    parsed = ParsedQuery(
        raw="Alice job in 2022",
        rewritten="Alice job in 2022",
        tokens=["Alice", "job", "2022"],
        keywords=["Alice", "job", "2022"],
        extensions={
            "schema_temporal": {
                "mode": "range",
                "event_from": "2022-01-01T00:00:00+00:00",
                "event_to": "2023-01-01T00:00:00+00:00",
            }
        },
    )

    selected, _units = selector.select(_SCOPE, parsed, [], limit=10)

    assert [candidate.unit_id for candidate in selected] == [direct.id]
    assert selector.last_result is not None
    assert selector.last_result.shrink_diagnostics is not None
    assert selector.last_result.shrink_diagnostics.timeline_neighbors_added == 0


def test_source_first_evidence_remains_retrievable_when_no_property_exists() -> None:
    source = _source("source-only", "entity-alice")
    domain = _FakeDomain([source])
    selector = TemporalEntitySelector(domain, source_fallback_enabled=True)

    selected, units = selector.select(
        _SCOPE,
        _parsed("Alice history"),
        [ScoredMemoryUnit(source, 1.0, RecallChannel.KEYWORD)],
    )

    assert [candidate.unit_id for candidate in selected] == [source.id]
    assert selected[0].unit is source
    assert units == {source.id: source}


def test_source_first_evidence_is_retained_when_direct_property_has_no_event_time() -> None:
    source = _source("source-relative", "entity-alice")
    source.segments = [Segment(content="Alice travelled last week")]
    property_unit = _property(
        "undated-property",
        value="Alice travelled",
        provenance=[source.id],
    )
    domain = _FakeDomain([source, property_unit], recalled_ids=[property_unit.id])
    selector = TemporalEntitySelector(domain, source_fallback_enabled=True)

    selected, _units = selector.select(
        _SCOPE,
        _parsed("When did Alice travel?"),
        [ScoredMemoryUnit(source, 1.0, RecallChannel.KEYWORD)],
        limit=2,
    )

    assert {candidate.unit_id for candidate in selected} == {source.id, property_unit.id}


def test_source_first_evidence_is_omitted_when_direct_property_fully_covers_it() -> None:
    source = _source("source-dated", "entity-alice")
    source.segments = [Segment(content="Alice travelled to Paris")]
    property_unit = _property(
        "dated-property",
        value="Alice travelled to Paris in August 2023",
        event_start="2023-08-01T00:00:00",
        event_end="2023-09-01T00:00:00",
        precision="month",
        provenance=[source.id],
    )
    domain = _FakeDomain([source, property_unit], recalled_ids=[property_unit.id])
    selector = TemporalEntitySelector(domain, source_fallback_enabled=True)

    selected, _units = selector.select(
        _SCOPE,
        _parsed("When did Alice travel?"),
        [ScoredMemoryUnit(source, 1.0, RecallChannel.KEYWORD)],
        limit=2,
    )

    assert [candidate.unit_id for candidate in selected] == [property_unit.id]


def test_pipeline_temporal_extension_returns_one_formal_entity_item() -> None:
    source = _source("source", "entity-alice")
    property_unit = _property(
        "property",
        value="Alice visited Paris in 2023",
        provenance=[source.id],
    )
    domain = _FakeDomain(
        [source, property_unit],
        recalled_ids=[source.id, property_unit.id],
    )
    selector = TemporalEntitySelector(domain)
    extension = SchemaTemporalPipelineExtension(selector, _PlainDiscloser())
    retriever = PipelineRetriever(
        _StaticParser(),
        RRFFuser(),
        _PlainDiscloser(),
        None,
        domain_store=domain,
        schema_temporal_extension=extension,
    )

    ordinary = retriever.retrieve(
        _SCOPE,
        RetrievalQuery(text="Paris", top_k=2, with_trajectory=True),
    )
    temporal = retriever.retrieve(
        _SCOPE,
        RetrievalQuery(
            text="Paris",
            top_k=2,
            with_trajectory=True,
            extensions={"schema_temporal": {"mode": "history"}},
        ),
    )

    assert [item.unit_id for item in ordinary.items] == [source.id, property_unit.id]
    assert "schema_temporal" not in [step.stage for step in ordinary.trajectory]
    temporal_stages = [step.stage for step in temporal.trajectory]
    assert temporal_stages.index("disclose") < temporal_stages.index("schema_temporal")
    assert [item.unit_id for item in temporal.items] == ["entity-alice", source.id]
    assert "Entity: alice" in temporal.items[0].content
    assert "Alice visited Paris in 2023" in temporal.items[0].content
    assert temporal.items[0].system_metadata["retrieval_view"] == "schema_temporal_entity"
    assert "message_time=unknown" in temporal.items[1].content
    assert temporal.items[1].system_metadata["retrieval_view"] == "schema_source_fallback"
    assert temporal.schema_temporal is not None
    assert temporal.schema_temporal.entities[0].entity_id == "entity-alice"


def test_pipeline_accepts_explicit_schema_temporal_query_field() -> None:
    property_unit = _property(
        "property",
        value="Alice visited Paris in 2023",
    )
    domain = _FakeDomain([property_unit], recalled_ids=[property_unit.id])
    extension = SchemaTemporalPipelineExtension(
        TemporalEntitySelector(domain),
        _PlainDiscloser(),
    )
    retriever = PipelineRetriever(
        _StaticParser(),
        RRFFuser(),
        _PlainDiscloser(),
        None,
        domain_store=domain,
        schema_temporal_extension=extension,
    )

    result = retriever.retrieve(
        _SCOPE,
        RetrievalQuery(
            text="Paris",
            top_k=1,
            schema_temporal=SchemaTemporalQuery(mode=SchemaTemporalMode.HISTORY),
        ),
    )

    assert [item.unit_id for item in result.items] == ["entity-alice"]
    assert result.schema_temporal is not None


def test_pipeline_formats_source_only_temporal_fallback_with_message_time() -> None:
    source = _source("source-only", "entity-alice")
    source.temporal.t_message = _dt("2024-05-06T00:00:00")
    domain = _FakeDomain([source], recalled_ids=[source.id])
    extension = SchemaTemporalPipelineExtension(
        TemporalEntitySelector(domain),
        _PlainDiscloser(),
    )
    retriever = PipelineRetriever(
        _StaticParser(),
        RRFFuser(),
        _PlainDiscloser(),
        None,
        domain_store=domain,
        schema_temporal_extension=extension,
    )

    result = retriever.retrieve(
        _SCOPE,
        RetrievalQuery(
            text="When did Alice travel?",
            top_k=1,
            schema_temporal=SchemaTemporalQuery(mode=SchemaTemporalMode.HISTORY),
        ),
    )

    assert [item.unit_id for item in result.items] == [source.id]
    assert "message_time=2024-05-06T00:00:00+00:00" in result.items[0].content
    assert result.items[0].system_metadata["retrieval_view"] == "schema_source_fallback"
