"""Schema TemporalEntity assembly, selection and fallback contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from time import perf_counter, sleep
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.type_def import (
    FilterOp,
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
    iter_clauses,
    matches_memory_unit,
)
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.discloser import Discloser
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.query_parser import QueryParser
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.schema_temporal import (
    EventTimePrecision,
    ExtractedTimeWindow,
    SchemaTemporalMode,
    SchemaTemporalQuery,
    SchemaTemporalReader,
    SchemaTemporalResult,
    SchemaTemporalShrinker,
    SchemaTimeExtractor,
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
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


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


def _is_schema_recall(query: ParsedQuery) -> bool:
    """Detect the schema Property filter pushed by the schema recall path."""

    for clause in iter_clauses(query.scalar_filters):
        if (
            clause.field == "system_metadata.extraction_mode"
            and clause.op is FilterOp.EQ
            and clause.value == "schema"
        ):
            return True
    return False


class _FakeDomain:
    """Minimal DomainStore-shaped fake with observable list and point reads."""

    def __init__(
        self,
        units: list[MemoryUnit],
        *,
        recalled_ids: list[str] | None = None,
        recall_delay_seconds: float = 0.0,
    ) -> None:
        self.units = {unit.id: unit for unit in units}
        self.recalled_ids = list(recalled_ids or [])
        self.recall_delay_seconds = float(recall_delay_seconds)
        self.get_calls: list[list[str]] = []
        self.recall_calls: list[dict] = []
        self.list_calls = 0
        self._recall_lock = Lock()

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
        return MemoryListResult(items=matched[offset:offset + limit], count=len(matched))

    def recall_and_get(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
    ) -> RecallResult[ScoredMemoryUnit]:
        started = perf_counter()
        if self.recall_delay_seconds > 0:
            sleep(self.recall_delay_seconds)
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
        ended = perf_counter()
        with self._recall_lock:
            self.recall_calls.append(
                {
                    "query": query,
                    "channels": list(channels or []),
                    "recall_limit": recall_limit,
                    "start": started,
                    "end": ended,
                    "duration": ended - started,
                    "is_schema": _is_schema_recall(query),
                }
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
    assert not hasattr(ordinary, "schema_temporal")
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


@pytest.mark.parametrize(
    "extension",
    [
        {"mode": "unknown"},
        {"mode": "snapshot"},
        {"fallback_policy": "unknown-policy"},
    ],
)
def test_invalid_temporal_extension_degrades_to_ordinary_results(extension: dict) -> None:
    source = _source("source", "entity-alice")
    domain = _FakeDomain([source], recalled_ids=[source.id])
    retriever = PipelineRetriever(
        _StaticParser(),
        RRFFuser(),
        _PlainDiscloser(),
        None,
        domain_store=domain,
        schema_temporal_extension=SchemaTemporalPipelineExtension(
            TemporalEntitySelector(domain), _PlainDiscloser()
        ),
    )

    result = retriever.retrieve(
        _SCOPE,
        RetrievalQuery(text="Alice", top_k=1, extensions={"schema_temporal": extension}),
    )

    assert [item.unit_id for item in result.items] == [source.id]
    assert not hasattr(result, "schema_temporal")
    assert len(result.errors) == 1
    assert result.errors[0].channel is RecallChannel.TEMPORAL
    assert result.errors[0].error_type == "ValueError"


def test_temporal_entity_keeps_single_slot_when_source_fallback_exists() -> None:
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
    retriever = PipelineRetriever(
        _StaticParser(),
        RRFFuser(),
        _PlainDiscloser(),
        None,
        domain_store=domain,
        schema_temporal_extension=SchemaTemporalPipelineExtension(
            selector, _PlainDiscloser()
        ),
    )

    result = retriever.retrieve(
        _SCOPE,
        RetrievalQuery(
            text="Paris",
            top_k=1,
            extensions={"schema_temporal": {"mode": "history"}},
        ),
    )

    assert [item.unit_id for item in result.items] == ["entity-alice"]
    assert result.items[0].system_metadata["retrieval_view"] == "schema_temporal_entity"
    assert [candidate.unit_id for candidate in selector.last_source_candidates] == [source.id]


@pytest.mark.parametrize("formatting_enabled", [False, True])
def test_pipeline_accepts_explicit_schema_temporal_query_field(
    formatting_enabled: bool,
) -> None:
    property_unit = _property(
        "property",
        value="Alice visited Paris in 2023",
    )
    domain = _FakeDomain([property_unit], recalled_ids=[property_unit.id])
    extension = SchemaTemporalPipelineExtension(
        TemporalEntitySelector(domain),
        _PlainDiscloser(),
        formatting_enabled=formatting_enabled,
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

    assert [item.unit_id for item in result.items] == [
        "entity-alice" if formatting_enabled else property_unit.id
    ]
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


def test_property_recall_runs_parallel_with_ordinary_recall() -> None:
    source = _source("source-parallel", "entity-alice")
    property_unit = _property(
        "property-parallel",
        value="Alice visited Paris in 2023",
        provenance=[source.id],
    )
    domain = _FakeDomain(
        [source, property_unit],
        recalled_ids=[source.id, property_unit.id],
        recall_delay_seconds=0.05,
    )
    retriever = PipelineRetriever(
        _StaticParser(),
        RRFFuser(),
        _PlainDiscloser(),
        None,
        domain_store=domain,
        schema_temporal_extension=SchemaTemporalPipelineExtension(
            TemporalEntitySelector(domain),
            _PlainDiscloser(),
        ),
    )
    query = RetrievalQuery(
        text="Paris",
        top_k=2,
        extensions={"schema_temporal": {"mode": "history"}},
    )

    started = perf_counter()
    retriever.retrieve(_SCOPE, query)
    elapsed = perf_counter() - started

    schema_calls = [call for call in domain.recall_calls if call["is_schema"]]
    ordinary_calls = [call for call in domain.recall_calls if not call["is_schema"]]
    assert len(schema_calls) >= 1, "Property 直达召回未发生"
    assert len(ordinary_calls) >= 1, "普通召回未发生"
    serial_cost = ordinary_calls[0]["duration"] + schema_calls[0]["duration"] - 0.03
    assert elapsed < serial_cost, (
        "总耗时 >= 普通召回 + Property 直达召回耗时（串行未并行）"
    )
    overlap = (
        ordinary_calls[0]["start"] < schema_calls[0]["end"]
        and schema_calls[0]["start"] < ordinary_calls[0]["end"]
    )
    assert overlap, "普通召回与 Property 直达召回的时间区间不重叠（串行未并行）"

    quiet_domain = _FakeDomain(
        [source, property_unit],
        recalled_ids=[source.id, property_unit.id],
    )
    quiet_retriever = PipelineRetriever(
        _StaticParser(),
        RRFFuser(),
        _PlainDiscloser(),
        None,
        domain_store=quiet_domain,
    )
    quiet_retriever.retrieve(_SCOPE, query)
    assert not any(call["is_schema"] for call in quiet_domain.recall_calls), (
        "未装配 schema_temporal 扩展时仍触发了 Property 直达召回"
    )


def test_reader_hydrates_entities_in_batch_not_per_entity() -> None:
    entity_ids = [f"entity-e{index}" for index in range(5)]
    ids_by_entity = {
        entity_id: [f"{entity_id}-p{position}" for position in range(2)]
        for entity_id in entity_ids
    }
    units_by_entity = {
        entity_id: [
            _property(unit_id, entity_key=entity_id, property_name=f"prop{position}")
            for position, unit_id in enumerate(unit_ids)
        ]
        for entity_id, unit_ids in ids_by_entity.items()
    }
    all_units = [unit for units in units_by_entity.values() for unit in units]

    class _CountingLookup:
        def __init__(self) -> None:
            self.single_calls: list[str] = []
            self.batch_calls: list[list[str]] = []

        def __call__(self, scope: Scope, entity_id: str) -> _Lookup:
            del scope
            self.single_calls.append(entity_id)
            return _Lookup(True, list(ids_by_entity[entity_id]))

        def lookup_batch(
            self,
            scope: Scope,
            entity_ids: list[str],
        ) -> dict[str, _Lookup]:
            del scope
            self.batch_calls.append(list(entity_ids))
            return {
                entity_id: _Lookup(True, list(ids_by_entity[entity_id]))
                for entity_id in entity_ids
            }

    lookup = _CountingLookup()
    domain = _FakeDomain(all_units)
    reader = SchemaTemporalReader(domain, property_id_lookup=lookup)

    result = reader.read(
        _SCOPE,
        entity_ids,
        SchemaTemporalQuery(mode=SchemaTemporalMode.HISTORY),
    )

    total_lookups = len(lookup.single_calls) + len(lookup.batch_calls)
    assert total_lookups <= 1, (
        f"property_id_lookup 调用次数 = {total_lookups}（逐实体）而非 ≤ 1（批量）"
    )
    assert len(domain.get_calls) <= 1, (
        f"domain.get 调用次数 = {len(domain.get_calls)}（逐实体）而非 ≤ 1（批量点读）"
    )
    assert [entity.entity_id for entity in result.entities] == entity_ids
    visible = {
        entity.entity_id: {
            entry.unit_id
            for timeline in entity.properties.values()
            for entry in timeline
        }
        for entity in result.entities
    }
    for entity_id in entity_ids:
        assert visible[entity_id] == set(ids_by_entity[entity_id])


def test_shrink_uses_local_clipping_before_per_entity_recall() -> None:
    counts = (3, 3, 1, 1, 1)
    units_by_entity: dict[str, list[MemoryUnit]] = {}
    entities = []
    for index, count in enumerate(counts):
        entity_id = f"entity-shrink-{index}"
        units = [
            _property(
                f"{entity_id}-p{position}",
                entity_key=entity_id,
                property_name=f"prop{position}",
                value=f"value {position}",
            )
            for position in range(count)
        ]
        units_by_entity[entity_id] = units
        entities.append(TemporalEntityAssembler().assemble(entity_id, units))
    result = SchemaTemporalResult(mode=SchemaTemporalMode.HISTORY, entities=entities)

    calls: list[list[str]] = []

    def _recall(entity_or_ids):
        if isinstance(entity_or_ids, str):
            entity_ids = [entity_or_ids]
            calls.append(entity_ids)
            return [
                SimpleNamespace(unit_id=unit.id, score=1.0)
                for unit in units_by_entity[entity_or_ids]
            ]
        entity_ids = list(entity_or_ids)
        calls.append(entity_ids)
        return {
            entity_id: [
                SimpleNamespace(unit_id=unit.id, score=1.0)
                for unit in units_by_entity[entity_id]
            ]
            for entity_id in entity_ids
        }

    shrinker = SchemaTemporalShrinker(
        max_properties_per_entity=1,
        allocation_min_factor=0.5,
        allocation_max_factor=1.5,
    )
    shrinker.shrink(result, _parsed("history"), entity_local_recall=_recall)

    assert len(calls) <= 1, (
        f"entity_local_recall 回调次数 = {len(calls)}（逐实体）而非 ≤ 1（批量）"
    )
    if len(calls) == 1:
        assert len(calls[0]) >= 2, "批量补召未一次覆盖多个超预算实体"
    assert len(result.entities) == 5
    for entity in result.entities:
        total = sum(len(timeline) for timeline in entity.properties.values())
        assert total <= 1, f"实体 {entity.entity_id} 保留 {total} 条，超过预算 1"


def test_reranker_called_batch_first_with_per_entity_fallback() -> None:
    """rerank 批量优先：正常路径 1 次调用覆盖全部超预算实体；批量失败时退化为逐实体
    重试，单实体失败只降级该实体，不影响其他实体。"""

    class _CountingReranker:
        def __init__(
            self,
            *,
            fail_batch: bool = False,
            fail_entity_index: int | None = None,
        ) -> None:
            self.calls: list[list[str]] = []
            self._fail_batch = fail_batch
            self._fail_entity_index = fail_entity_index
            self._entity_retries = 0

        def rerank(self, query: str, texts: list[str]) -> list[float]:
            del query
            self.calls.append(list(texts))
            if self._fail_batch and len(self.calls) == 1:
                raise RuntimeError("simulated batch rerank failure")
            if self._fail_entity_index is not None and len(self.calls) > 1:
                self._entity_retries += 1
                if self._entity_retries == self._fail_entity_index + 1:
                    raise RuntimeError("simulated per-entity rerank failure")
            return [0.5 for _ in texts]

    entities = []
    for index in range(5):
        entity_id = f"entity-rerank-{index}"
        units = [
            _property(
                f"{entity_id}-p{position}",
                entity_key=entity_id,
                property_name=f"prop{position}",
                value=f"value {position}",
            )
            for position in range(3)
        ]
        entities.append(TemporalEntityAssembler().assemble(entity_id, units))
    result = SchemaTemporalResult(mode=SchemaTemporalMode.HISTORY, entities=entities)

    reranker = _CountingReranker()
    shrinker = SchemaTemporalShrinker(
        max_properties_per_entity=1,
        reranker=reranker,
        rerank_enabled=True,
    )
    shrinker.shrink(result, _parsed("history"))
    assert len(reranker.calls) == 1, (
        f"正常路径 reranker.rerank 调用次数 = {len(reranker.calls)}（应为批量 1 次）"
    )
    assert result.shrink_diagnostics is not None
    assert result.shrink_diagnostics.entities_reranked == 5
    for entity in result.entities:
        total = sum(len(timeline) for timeline in entity.properties.values())
        assert total <= 1, f"实体 {entity.entity_id} 保留 {total} 条，超过预算 1"

    entities2 = []
    for index in range(5):
        entity_id = f"entity-rerank-fail-{index}"
        units = [
            _property(
                f"{entity_id}-p{position}",
                entity_key=entity_id,
                property_name=f"prop{position}",
                value=f"value {position}",
            )
            for position in range(3)
        ]
        entities2.append(TemporalEntityAssembler().assemble(entity_id, units))
    result2 = SchemaTemporalResult(mode=SchemaTemporalMode.HISTORY, entities=entities2)
    failing_reranker = _CountingReranker(fail_batch=True, fail_entity_index=0)
    shrinker2 = SchemaTemporalShrinker(
        max_properties_per_entity=1,
        reranker=failing_reranker,
        rerank_enabled=True,
    )
    shrinker2.shrink(result2, _parsed("history"))
    # 1 次批量失败 + 5 次逐实体重试
    assert len(failing_reranker.calls) == 6, (
        f"失败路径调用次数 = {len(failing_reranker.calls)}（应为 1 批 + 5 逐实体 = 6）"
    )
    assert result2.shrink_diagnostics is not None
    assert "RuntimeError" in result2.shrink_diagnostics.degraded_errors
    assert len(result2.entities) == 5
    for entity in result2.entities:
        total = sum(len(timeline) for timeline in entity.properties.values())
        assert total <= 1, f"实体 {entity.entity_id} 保留 {total} 条，超过预算 1"


def test_default_off_is_byte_identical() -> None:
    source = _source("source-default-off", "entity-alice")
    property_unit = _property(
        "property-default-off",
        value="Alice visited Paris in 2023",
        provenance=[source.id],
    )
    domain = _FakeDomain(
        [source, property_unit],
        recalled_ids=[source.id, property_unit.id],
    )
    off = PipelineRetriever(
        _StaticParser(),
        RRFFuser(),
        _PlainDiscloser(),
        None,
        domain_store=domain,
    )
    on = PipelineRetriever(
        _StaticParser(),
        RRFFuser(),
        _PlainDiscloser(),
        None,
        domain_store=domain,
        schema_temporal_extension=SchemaTemporalPipelineExtension(
            TemporalEntitySelector(domain),
            _PlainDiscloser(),
        ),
    )
    query = RetrievalQuery(text="Paris", top_k=2, with_trajectory=True)

    off_result = off.retrieve(_SCOPE, query)
    on_result = on.retrieve(_SCOPE, query)

    off_payloads = [
        (item.unit_id, item.score, item.level, item.content) for item in off_result.items
    ]
    on_payloads = [
        (item.unit_id, item.score, item.level, item.content) for item in on_result.items
    ]
    assert on_payloads == off_payloads, "默认关闭路径的输出与启用扩展时不一致"
    assert "schema_temporal" not in [step.stage for step in on_result.trajectory]
    assert not hasattr(on_result, "schema_temporal")
    assert "schema_temporal" not in vars(on_result)


def test_watermark_invalidation_takes_effect_immediately() -> None:
    """无进程内缓存时，invalidate_scope 后下一次 lookup 立即回退到 indexed=False。"""

    from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)

    unit = _property("p-invalidate", entity_key="entity-inv", property_name="occ")
    index.upsert([unit])

    assert index.lookup(_SCOPE, "entity-inv").indexed is True

    index.invalidate_scope(_SCOPE)
    assert index.lookup(_SCOPE, "entity-inv").indexed is False


def test_read_entities_single_entity_uses_narrow_lookup() -> None:
    """单实体读走 lookup（窄前缀），不走 lookup_batch（全 Scope 扫描）。"""

    entity_id = "entity-single"
    unit = _property("p-single", entity_key=entity_id, property_name="occ")
    domain = _FakeDomain([unit])

    narrow_calls: list[str] = []
    batch_calls: list[list[str]] = []

    class _SplitLookup:
        def __call__(self, scope: Scope, eid: str) -> _Lookup:
            del scope
            narrow_calls.append(eid)
            return _Lookup(True, [unit.id])

        def lookup_batch(self, scope: Scope, entity_ids: list[str]) -> dict[str, _Lookup]:
            del scope
            batch_calls.append(list(entity_ids))
            return {eid: _Lookup(True, [unit.id]) for eid in entity_ids}

    reader = SchemaTemporalReader(domain, property_id_lookup=_SplitLookup())
    result = reader.read(
        _SCOPE,
        [entity_id],
        SchemaTemporalQuery(mode=SchemaTemporalMode.HISTORY),
    )

    assert len(narrow_calls) == 1, "单实体应走窄前缀 lookup"
    assert batch_calls == [], "单实体不应触发 lookup_batch 全 Scope 扫描"
    assert len(result.entities) == 1
    assert result.entities[0].entity_id == entity_id


def test_read_entities_plain_list_lookup_form_is_supported() -> None:
    """lookup_batch 返回 plain list（而非 SchemaPropertyLookup）时不能静默丢数据。"""

    entity_ids = [f"entity-plain-{i}" for i in range(3)]
    units = [
        _property(f"p-plain-{i}", entity_key=eid, property_name="occ")
        for i, eid in enumerate(entity_ids)
    ]
    domain = _FakeDomain(units)
    ids_by_entity = {eid: [u.id] for eid, u in zip(entity_ids, units, strict=True)}

    class _PlainListBatchLookup:
        def __call__(self, scope: Scope, eid: str):
            del scope
            return ids_by_entity[eid]

        def lookup_batch(self, scope: Scope, eids: list[str]) -> dict[str, list[str]]:
            del scope
            return {eid: ids_by_entity[eid] for eid in eids}

    reader = SchemaTemporalReader(domain, property_id_lookup=_PlainListBatchLookup())
    result = reader.read(
        _SCOPE,
        entity_ids,
        SchemaTemporalQuery(mode=SchemaTemporalMode.HISTORY),
    )

    visible = {
        entity.entity_id: {
            entry.unit_id for timeline in entity.properties.values() for entry in timeline
        }
        for entity in result.entities
    }
    for eid in entity_ids:
        assert visible.get(eid) == set(ids_by_entity[eid]), (
            f"实体 {eid} 在 plain-list 返回形态下丢数据"
        )


def test_recall_errors_are_thread_local_across_concurrent_searches() -> None:
    """同一 SchemaDualPathRecaller 实例在两个线程里的 last_errors 互不串扰。"""

    from threading import Thread

    recaller = SchemaDualPathRecaller(_FakeDomain([]))
    errors_a: list[int] = []
    errors_b: list[int] = []

    def _worker(tag: str, sink: list[int]) -> None:
        recaller.reset_errors()
        from jiuwen_memory.common.type_def import ChannelError, RecallChannel

        recaller.add_errors(
            [ChannelError(RecallChannel.TEMPORAL, f"src-{tag}", "Err", "x")]
        )
        sleep(0.02)
        sink.append(len(recaller.last_errors))

    t_a = Thread(target=_worker, args=("a", errors_a))
    t_b = Thread(target=_worker, args=("b", errors_b))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    assert errors_a == [1], f"线程 A 的错误被串扰：{errors_a}"
    assert errors_b == [1], f"线程 B 的错误被串扰：{errors_b}"


def test_parallel_recall_times_out_and_degrades() -> None:
    """Property 直达召回超过 schema_temporal_recall_timeout 时降级为 TEMPORAL 错误，
    普通结果仍正常返回（search 串行重跑 Property 召回）。"""

    source = _source("source-timeout", "entity-alice")
    property_unit = _property(
        "property-timeout",
        value="Alice visited Paris in 2023",
        provenance=[source.id],
    )

    class _SlowSchemaDomain(_FakeDomain):
        def recall_and_get(self, scope, query, *, channels, recall_limit):
            if _is_schema_recall(query):
                sleep(0.3)
            return super().recall_and_get(
                scope, query, channels=channels, recall_limit=recall_limit
            )

    domain = _SlowSchemaDomain(
        [source, property_unit],
        recalled_ids=[source.id, property_unit.id],
    )
    retriever = PipelineRetriever(
        _StaticParser(),
        RRFFuser(),
        _PlainDiscloser(),
        None,
        domain_store=domain,
        schema_temporal_extension=SchemaTemporalPipelineExtension(
            TemporalEntitySelector(domain),
            _PlainDiscloser(),
        ),
        schema_temporal_recall_timeout=0.05,
    )
    query = RetrievalQuery(
        text="Paris",
        top_k=2,
        extensions={"schema_temporal": {"mode": "history"}},
    )

    result = retriever.retrieve(_SCOPE, query)

    temporal_errors = [
        error for error in result.errors if error.channel == RecallChannel.TEMPORAL
    ]
    assert any(
        "timeout" in error.message.lower() or "Timeout" in error.error_type
        for error in temporal_errors
    ), (
        f"未记录超时降级错误：{temporal_errors}"
    )


def test_parallel_recall_reuses_shared_executor() -> None:
    """多次 retrieve 复用同一个共享 executor，不每请求新建。"""

    source = _source("source-reuse", "entity-alice")
    property_unit = _property(
        "property-reuse",
        value="Alice visited Paris in 2023",
        provenance=[source.id],
    )
    domain = _FakeDomain(
        [source, property_unit],
        recalled_ids=[source.id, property_unit.id],
    )
    retriever = PipelineRetriever(
        _StaticParser(),
        RRFFuser(),
        _PlainDiscloser(),
        None,
        domain_store=domain,
        schema_temporal_extension=SchemaTemporalPipelineExtension(
            TemporalEntitySelector(domain),
            _PlainDiscloser(),
        ),
    )
    query = RetrievalQuery(
        text="Paris",
        top_k=2,
        extensions={"schema_temporal": {"mode": "history"}},
    )

    assert retriever._schema_temporal_executor is None
    retriever.retrieve(_SCOPE, query)
    first = retriever._schema_temporal_executor
    assert first is not None, "首次 retrieve 后共享 executor 未创建"
    retriever.retrieve(_SCOPE, query)
    assert retriever._schema_temporal_executor is first, "第二次 retrieve 新建了 executor"


def test_lookup_batch_uses_narrow_scan_below_threshold() -> None:
    """实体数 ≤ lookup_batch_threshold 时按每个实体走窄前缀 lookup，不全 Scope 扫描。"""

    from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv, lookup_batch_threshold=8)
    for entity_index in range(3):
        index.upsert([
            _property(
                f"p-batch-{entity_index}",
                entity_key=f"entity-batch-{entity_index}",
                property_name="occ",
            )
        ])

    narrow_calls: list[str] = []
    original_lookup = index.lookup

    def _spied_lookup(scope: Scope, entity_key: str):
        narrow_calls.append(entity_key)
        return original_lookup(scope, entity_key)

    index.lookup = _spied_lookup  # type: ignore[method-assign]

    result = index.lookup_batch(_SCOPE, ["entity-batch-0", "entity-batch-1", "entity-batch-2"])

    assert len(narrow_calls) == 3, (
        f"≤阈值时应走窄前缀 lookup 3 次，实际 {len(narrow_calls)}"
    )
    assert all(
        result[eid].indexed for eid in ["entity-batch-0", "entity-batch-1", "entity-batch-2"]
    )


def test_lookup_batch_full_scan_above_threshold() -> None:
    """实体数 > lookup_batch_threshold 时走一次全 Scope scan。"""

    from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv, lookup_batch_threshold=3)
    entity_keys = [f"entity-full-{i}" for i in range(5)]
    for entity_index, entity_key in enumerate(entity_keys):
        index.upsert([
            _property(
                f"p-full-{entity_index}",
                entity_key=entity_key,
                property_name="occ",
            )
        ])

    narrow_calls: list[str] = []
    original_lookup = index.lookup

    def _spied_lookup(scope: Scope, entity_key: str):
        narrow_calls.append(entity_key)
        return original_lookup(scope, entity_key)

    index.lookup = _spied_lookup  # type: ignore[method-assign]

    result = index.lookup_batch(_SCOPE, entity_keys)

    assert narrow_calls == [], (
        f">阈值时应走全 Scope scan 而非窄前缀，实际窄前缀调用 {narrow_calls}"
    )
    assert all(result[eid].indexed and len(result[eid].unit_ids) == 1 for eid in entity_keys)


def test_read_entities_falls_back_when_indexed_but_empty() -> None:
    """lookup_batch 返回 indexed=True 但 unit_ids 为空时，reader 回退 _list_properties
    真源复核，避免漏召（并发写期间 membership 被删但 watermark 仍 complete 的窗口）。"""

    entity_ids = [f"entity-empty-{i}" for i in range(3)]
    units = [
        _property(f"p-empty-{i}", entity_key=eid, property_name="occ")
        for i, eid in enumerate(entity_ids)
    ]
    domain = _FakeDomain(units)

    class _EmptyBatchLookup:
        def __call__(self, scope: Scope, eid: str):
            del scope
            return _Lookup(True, [])

        def lookup_batch(self, scope: Scope, eids: list[str]) -> dict[str, _Lookup]:
            del scope
            return {eid: _Lookup(True, []) for eid in eids}

    reader = SchemaTemporalReader(domain, property_id_lookup=_EmptyBatchLookup())
    result = reader.read(
        _SCOPE,
        entity_ids,
        SchemaTemporalQuery(mode=SchemaTemporalMode.HISTORY),
    )

    assert domain.list_calls > 0, "indexed=True 但空时应回退 _list_properties 真源复核"
    visible = {
        entity.entity_id: {
            entry.unit_id for timeline in entity.properties.values() for entry in timeline
        }
        for entity in result.entities
    }
    for i, eid in enumerate(entity_ids):
        assert visible.get(eid) == {units[i].id}, f"实体 {eid} 回退 list 后丢数据"


@pytest.mark.unit
def test_extractor_invalid_month_range_returns_empty_window() -> None:
    """「2026年13月」非法年月：区间分支返回空窗，不让 ValueError 穿透检索链路。"""
    extractor = SchemaTimeExtractor(now=lambda: datetime(2026, 6, 16, 12, 0, tzinfo=UTC))

    result = extractor.extract("2026年13月到2027年2月的会议")

    assert result == ExtractedTimeWindow()
    assert result.found is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "过去9999999999天",
        "过去200000周",
        "last 1000000000 days",
    ],
)
def test_extractor_relative_time_overflow_returns_empty_window(text: str) -> None:
    """相对时间 N 溢出：语义≈全部历史，返回空窗而非 OverflowError 穿透。"""
    extractor = SchemaTimeExtractor(now=lambda: datetime(2026, 6, 16, 12, 0, tzinfo=UTC))

    result = extractor.extract(text)

    assert result == ExtractedTimeWindow()
    assert result.found is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "过去" + "9" * 5000 + "天",       # int(str) 4300 位上限 → ValueError
        "2020年到9999年的会议",           # 区间右端 9999 年 → _next_year 越界 ValueError
        "9999年12月",                     # chinese 单日期 → _exclusive_end 越界 ValueError
        "9999-12-31",                     # iso 单日期 → DAY 精度加法越过 datetime.max OverflowError
        "2020-01到9999-12",               # iso 区间 → 右端 _exclusive_end 越界 ValueError
    ],
)
def test_extractor_boundary_years_and_huge_digits_return_empty_window(text: str) -> None:
    """年份越界（9999）、超长数字串（int 4300 位上限）与 timedelta 溢出同构空窗回落。"""
    extractor = SchemaTimeExtractor(now=lambda: datetime(2026, 6, 16, 12, 0, tzinfo=UTC))

    result = extractor.extract(text)

    assert result == ExtractedTimeWindow()
    assert result.found is False


@pytest.mark.unit
@pytest.mark.parametrize("text", ["December 9999", "Dec 31, 9999"])
def test_extractor_english_max_date_returns_empty_window(text: str) -> None:
    """英文日期 9999 年：排他端越界（ValueError/OverflowError）同构空窗回落。"""
    extractor = SchemaTimeExtractor(now=lambda: datetime(2026, 6, 16, 12, 0, tzinfo=UTC))

    result = extractor.extract(text)

    assert result == ExtractedTimeWindow()
    assert result.found is False


@pytest.mark.unit
def test_extractor_relative_overflow_falls_through_to_later_rules() -> None:
    """相对时间溢出后继续后续相对规则（「最近1000000000天，今天」应取今天窗）。"""
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    extractor = SchemaTimeExtractor(now=lambda: now)

    result = extractor.extract("最近1000000000天，今天")

    assert result.found is True
    assert result.start == datetime(2026, 6, 16, 0, 0, tzinfo=UTC)
    assert result.end == datetime(2026, 6, 17, 0, 0, tzinfo=UTC)
