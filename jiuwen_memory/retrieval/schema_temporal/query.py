# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolve explicit and parsed temporal inputs into one schema query plan."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from jiuwen_memory.common.type_def import ParsedQuery

from .model import EventTimePrecision, SchemaTemporalMode, SchemaTemporalQuery
from .time_extractor import SchemaTimeExtractor, TemporalIntentSource


@dataclass(frozen=True, slots=True)
class TemporalQueryPlan:
    """Single source of truth for schema event-time and knowledge-time selection."""

    query: SchemaTemporalQuery
    source: TemporalIntentSource = TemporalIntentSource.NONE
    precision: EventTimePrecision = EventTimePrecision.UNKNOWN

    @property
    def enabled(self) -> bool:
        return self.source is not TemporalIntentSource.NONE


def resolve_temporal_query(
    explicit: SchemaTemporalQuery | None,
    parsed: ParsedQuery,
    *,
    auto_enabled: bool,
) -> TemporalQueryPlan:
    if explicit is not None:
        explicit = _inherit_retrieval_controls(explicit, parsed)
        return TemporalQueryPlan(
            query=explicit,
            source=TemporalIntentSource.EXPLICIT,
            precision=explicit.event_precision,
        )
    if auto_enabled:
        window = _auto_time_window(parsed)
        if window is None:
            return TemporalQueryPlan(
                query=SchemaTemporalQuery(),
                source=TemporalIntentSource.NONE,
            )
        start, end, precision, source = window
        return TemporalQueryPlan(
            query=SchemaTemporalQuery(
                mode=SchemaTemporalMode.RANGE,
                event_from=start,
                event_to=end,
                event_precision=precision,
                knowledge_as_of=parsed.as_of,
                include_archived=parsed.include_archived,
            ),
            source=source,
            precision=precision,
        )
    return TemporalQueryPlan(query=SchemaTemporalQuery(), source=TemporalIntentSource.NONE)


def _inherit_retrieval_controls(
    explicit: SchemaTemporalQuery,
    parsed: ParsedQuery,
) -> SchemaTemporalQuery:
    """Keep typed temporal input consistent with the enclosing retrieval query."""

    knowledge_as_of = explicit.knowledge_as_of or parsed.as_of
    include_archived = explicit.include_archived or parsed.include_archived
    if (
        knowledge_as_of == explicit.knowledge_as_of
        and include_archived == explicit.include_archived
    ):
        return explicit
    return replace(
        explicit,
        knowledge_as_of=knowledge_as_of,
        include_archived=include_archived,
    )


def _auto_time_window(
    parsed: ParsedQuery,
) -> (
    tuple[
        datetime | None,
        datetime | None,
        EventTimePrecision,
        TemporalIntentSource,
    ]
    | None
):
    if parsed.time_from is not None or parsed.time_to is not None:
        return (
            parsed.time_from,
            parsed.time_to,
            _parsed_precision(parsed),
            _parsed_source(parsed),
        )
    extracted = SchemaTimeExtractor().extract(parsed.raw)
    if not extracted.found:
        return None
    return extracted.start, extracted.end, extracted.precision, extracted.source


def temporal_query_from_extensions(parsed: ParsedQuery) -> SchemaTemporalQuery | None:
    """Read the opt-in schema temporal DSL from parsed query extensions."""

    raw = parsed.extensions.get("schema_temporal")
    if isinstance(raw, SchemaTemporalQuery):
        return raw
    if isinstance(raw, dict):
        values = dict(raw)
        values.setdefault("knowledge_as_of", parsed.as_of)
        values.setdefault("include_archived", parsed.include_archived)
        return SchemaTemporalQuery.from_mapping(values)
    if isinstance(raw, str) and raw.strip():
        return SchemaTemporalQuery(
            mode=raw.strip(),
            knowledge_as_of=parsed.as_of,
            include_archived=parsed.include_archived,
        )
    if raw is True:
        return SchemaTemporalQuery(
            mode=SchemaTemporalMode.LATEST,
            knowledge_as_of=parsed.as_of,
            include_archived=parsed.include_archived,
        )
    return None


def _parsed_source(parsed: ParsedQuery) -> TemporalIntentSource:
    value = parsed.extensions.get("time_source", TemporalIntentSource.RULE.value)
    try:
        return TemporalIntentSource(str(value))
    except ValueError:
        return TemporalIntentSource.RULE


def _parsed_precision(parsed: ParsedQuery) -> EventTimePrecision:
    raw = parsed.extensions.get("time_precision")
    if raw is not None:
        try:
            return EventTimePrecision(str(raw))
        except ValueError:
            pass
    start = parsed.time_from
    end = parsed.time_to
    if start is None or end is None:
        return EventTimePrecision.UNKNOWN
    if start.hour == start.minute == start.second == start.microsecond == 0:
        if start.month == 1 and start.day == 1 and end == start.replace(year=start.year + 1):
            return EventTimePrecision.YEAR
        next_month = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
        if start.day == 1 and end == next_month:
            return EventTimePrecision.MONTH
        if end - start == timedelta(days=1):
            return EventTimePrecision.DAY
    return EventTimePrecision.DATETIME
