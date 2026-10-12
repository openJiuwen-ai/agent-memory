# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Read-only temporal views assembled from schema property MemoryUnits."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from jiuwen_memory.common.schema_property import (
    schema_entity_fallback_key,
    schema_property_entity_id,
)
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit


class EventTimePrecision(str, Enum):
    """Precision retained for an event-time half-open interval."""

    UNKNOWN = "unknown"
    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    DATETIME = "datetime"


class SchemaTemporalMode(str, Enum):
    """Shape requested from an entity property timeline."""

    LATEST = "latest"
    SNAPSHOT = "snapshot"
    HISTORY = "history"
    RANGE = "range"


class TemporalFallbackPolicy(str, Enum):
    """Policy used when an event-time selector finds no property values."""

    NONE = "none"
    FULL_TIMELINE = "full_timeline"


@dataclass(frozen=True, slots=True)
class SchemaTemporalQuery:
    """Two-axis query over schema properties.

    Event fields address when a fact happened. ``knowledge_as_of`` addresses
    when the system considered the persisted MemoryUnit valid.
    """

    mode: SchemaTemporalMode = SchemaTemporalMode.LATEST
    event_at: datetime | None = None
    knowledge_as_of: datetime | None = None
    event_from: datetime | None = None
    event_to: datetime | None = None
    event_precision: EventTimePrecision = EventTimePrecision.UNKNOWN
    property_names: tuple[str, ...] = ()
    include_undated: bool | None = None
    include_archived: bool = False
    fallback_policy: TemporalFallbackPolicy = TemporalFallbackPolicy.NONE
    entity_limit: int = 20
    per_entity_limit: int = 1000

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _enum_value(SchemaTemporalMode, self.mode))
        object.__setattr__(
            self,
            "fallback_policy",
            _enum_value(TemporalFallbackPolicy, self.fallback_policy),
        )
        object.__setattr__(
            self,
            "event_precision",
            _event_precision(self.event_precision),
        )
        for field_name in ("event_at", "knowledge_as_of", "event_from", "event_to"):
            object.__setattr__(self, field_name, _aware(getattr(self, field_name)))
        if self.include_undated is None:
            default = self.mode in {SchemaTemporalMode.LATEST, SchemaTemporalMode.HISTORY}
            object.__setattr__(self, "include_undated", default)
        names: list[str] = []
        for value in self.property_names:
            name = str(value).strip()
            if name and name not in names:
                names.append(name)
        object.__setattr__(self, "property_names", tuple(names))
        if self.entity_limit < 1:
            raise ValueError("schema temporal entity_limit must be positive")
        if self.per_entity_limit < 1:
            raise ValueError("schema temporal per_entity_limit must be positive")
        if self.event_from and self.event_to and self.event_from >= self.event_to:
            raise ValueError("schema temporal event_from must be < event_to")
        if self.mode is SchemaTemporalMode.SNAPSHOT and self.event_at is None:
            raise ValueError("schema temporal snapshot mode requires event_at")
        if self.mode is SchemaTemporalMode.RANGE and not (self.event_from or self.event_to):
            raise ValueError("schema temporal range mode requires event_from or event_to")

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> SchemaTemporalQuery:
        """Build a transport-friendly query from ``RetrievalQuery.extensions``."""

        property_names = value.get("property_names")
        if isinstance(property_names, str):
            names = (property_names,)
        elif isinstance(property_names, (list, tuple)):
            names = tuple(str(item) for item in property_names)
        else:
            names = ()
        include_undated = value.get("include_undated")
        return cls(
            mode=str(value.get("mode") or SchemaTemporalMode.LATEST.value),
            event_at=_parse_datetime(value.get("event_at")),
            knowledge_as_of=_parse_datetime(value.get("knowledge_as_of")),
            event_from=_parse_datetime(value.get("event_from")),
            event_to=_parse_datetime(value.get("event_to")),
            event_precision=str(value.get("event_precision") or EventTimePrecision.UNKNOWN.value),
            property_names=names,
            include_undated=(
                _coerce_bool(include_undated, default=False)
                if include_undated is not None
                else None
            ),
            include_archived=_coerce_bool(value.get("include_archived"), default=False),
            fallback_policy=str(value.get("fallback_policy") or TemporalFallbackPolicy.NONE.value),
            entity_limit=_positive_int(value.get("entity_limit"), 20),
            per_entity_limit=_positive_int(value.get("per_entity_limit"), 1000),
        )


@dataclass(frozen=True, slots=True)
class SchemaPropertyEntry:
    """One persisted schema property fact and its two time axes."""

    unit_id: str
    entity_id: str
    property_name: str
    value: str
    event_time: datetime | None
    event_precision: EventTimePrecision
    event_start: datetime | None
    event_end: datetime | None
    message_time: datetime | None
    ingest_time: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    lifecycle: LifecycleState
    supersedes: str = ""
    provenance: tuple[str, ...] = ()

    @classmethod
    def from_unit(cls, unit: MemoryUnit) -> SchemaPropertyEntry:
        metadata = unit.system_metadata
        event_time = _aware(unit.temporal.t_event)
        precision = _event_precision(
            metadata.get("schema_event_precision")
            or getattr(unit.temporal, "t_event_precision", None)
        )
        if precision is EventTimePrecision.UNKNOWN and event_time is not None:
            precision = EventTimePrecision.DATETIME
        event_start = _parse_datetime(
            metadata.get("schema_event_start") or getattr(unit.temporal, "t_event_start", None)
        )
        event_start = event_start or event_time
        event_end = _parse_datetime(
            metadata.get("schema_event_end") or getattr(unit.temporal, "t_event_end", None)
        )
        if event_end is None:
            event_end = _exclusive_end(event_start, precision)
        return cls(
            unit_id=unit.id,
            entity_id=_unit_entity_id(unit),
            property_name=str(metadata.get("schema_property_name") or "").strip(),
            value=unit.content,
            event_time=event_time,
            event_precision=precision,
            event_start=event_start,
            event_end=event_end,
            message_time=_aware(unit.temporal.t_message),
            ingest_time=_aware(unit.temporal.t_ingest),
            valid_from=_aware(unit.temporal.t_valid),
            valid_to=_aware(unit.temporal.t_invalid),
            lifecycle=unit.lifecycle,
            supersedes=unit.supersedes,
            provenance=tuple(unit.provenance),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "entity_id": self.entity_id,
            "property_name": self.property_name,
            "value": self.value,
            "event_time": _iso(self.event_time),
            "event_precision": self.event_precision.value,
            "event_start": _iso(self.event_start),
            "event_end": _iso(self.event_end),
            "message_time": _iso(self.message_time),
            "ingest_time": _iso(self.ingest_time),
            "valid_from": _iso(self.valid_from),
            "valid_to": _iso(self.valid_to),
            "lifecycle": self.lifecycle.value,
            "supersedes": self.supersedes,
            "provenance": list(self.provenance),
        }
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SchemaPropertyEntry:
        return cls(
            unit_id=str(data.get("unit_id") or ""),
            entity_id=str(data.get("entity_id") or ""),
            property_name=str(data.get("property_name") or ""),
            value=str(data.get("value") or ""),
            event_time=_parse_datetime(data.get("event_time")),
            event_precision=_event_precision(data.get("event_precision")),
            event_start=_parse_datetime(data.get("event_start")),
            event_end=_parse_datetime(data.get("event_end")),
            message_time=_parse_datetime(data.get("message_time")),
            ingest_time=_parse_datetime(data.get("ingest_time")),
            valid_from=_parse_datetime(data.get("valid_from")),
            valid_to=_parse_datetime(data.get("valid_to")),
            lifecycle=_lifecycle(data.get("lifecycle")),
            supersedes=str(data.get("supersedes") or ""),
            provenance=tuple(str(item) for item in data.get("provenance", [])),
        )


def schema_unit_known_at(unit: MemoryUnit, query: SchemaTemporalQuery) -> bool:
    """Apply schema history visibility to a persisted Property MemoryUnit.

    The generic retrieval contract treats ``include_archived`` as archived-only.
    Schema property history also needs superseded versions produced by Property
    Merge, while forgotten units remain unavailable.
    """

    if unit.lifecycle is LifecycleState.FORGOTTEN:
        return False
    if query.knowledge_as_of is None:
        if unit.lifecycle is not LifecycleState.ACTIVE:
            return query.include_archived and unit.lifecycle in {
                LifecycleState.ARCHIVED,
                LifecycleState.SUPERSEDED,
            }
        knowledge_time = datetime.now(timezone.utc)
    else:
        knowledge_time = query.knowledge_as_of
    temporal = unit.temporal
    if temporal.t_valid is not None and knowledge_time < _aware(temporal.t_valid):
        return False
    return temporal.t_invalid is None or knowledge_time < _aware(temporal.t_invalid)


@dataclass(frozen=True, slots=True)
class SchemaShrinkDiagnostics:
    """Observable outcome of query-aware property shrinking and late fusion."""

    entries_before: int = 0
    entries_after: int = 0
    entities_reranked: int = 0
    direct_properties_reranked: int = 0
    final_entities_reranked: int = 0
    direct_entries_retained: int = 0
    timeline_neighbors_added: int = 0
    degraded_errors: tuple[str, ...] = ()


@dataclass(slots=True)
class TemporalEntity:
    """Canonical entity with independent, timestamp-ordered property timelines."""

    entity_id: str
    name: str = ""
    entity_type: str = ""
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    search_fields: list[str] = field(default_factory=list)
    properties: dict[str, list[SchemaPropertyEntry]] = field(default_factory=dict)
    fallback_used: bool = False
    truncated: bool = False

    def add(self, entry: SchemaPropertyEntry) -> None:
        if not entry.property_name:
            return
        timeline = self.properties.setdefault(entry.property_name, [])
        key = _entry_sort_key(entry)
        keys = [_entry_sort_key(item) for item in timeline]
        timeline.insert(bisect.bisect_right(keys, key), entry)

    def timeline(
        self,
        property_name: str,
        *,
        knowledge_as_of: datetime | None = None,
        include_archived: bool = False,
        include_undated: bool = True,
    ) -> list[SchemaPropertyEntry]:
        result: list[SchemaPropertyEntry] = []
        for entry in self.properties.get(property_name, []):
            if not _known_at(entry, knowledge_as_of, include_archived):
                continue
            if include_undated or _event_anchor(entry) is not None:
                result.append(entry)
        return result

    def latest(
        self,
        *,
        knowledge_as_of: datetime | None = None,
        property_names: tuple[str, ...] = (),
        include_archived: bool = False,
        include_undated: bool = True,
    ) -> dict[str, SchemaPropertyEntry]:
        result: dict[str, SchemaPropertyEntry] = {}
        for name in self._selected_names(property_names):
            entries = self.timeline(
                name,
                knowledge_as_of=knowledge_as_of,
                include_archived=include_archived,
                include_undated=include_undated,
            )
            if entries:
                result[name] = max(entries, key=_latest_sort_key)
        return result

    def snapshot(
        self,
        event_at: datetime,
        *,
        knowledge_as_of: datetime | None = None,
        property_names: tuple[str, ...] = (),
        include_archived: bool = False,
        include_undated: bool = False,
    ) -> dict[str, SchemaPropertyEntry]:
        target = _aware(event_at)
        if target is None:
            return {}
        result: dict[str, SchemaPropertyEntry] = {}
        for name in self._selected_names(property_names):
            entries = self.timeline(
                name,
                knowledge_as_of=knowledge_as_of,
                include_archived=include_archived,
                include_undated=include_undated,
            )
            dated = [
                entry
                for entry in entries
                if _event_anchor(entry) is not None and _event_anchor(entry) <= target
            ]
            if dated:
                result[name] = max(dated, key=_entry_sort_key)
                continue
            if include_undated:
                undated = [entry for entry in entries if _event_anchor(entry) is None]
                if undated:
                    result[name] = max(undated, key=_latest_sort_key)
        return result

    def history(
        self,
        *,
        knowledge_as_of: datetime | None = None,
        property_names: tuple[str, ...] = (),
        include_archived: bool = False,
        include_undated: bool = True,
    ) -> dict[str, list[SchemaPropertyEntry]]:
        result: dict[str, list[SchemaPropertyEntry]] = {}
        for name in self._selected_names(property_names):
            entries = self.timeline(
                name,
                knowledge_as_of=knowledge_as_of,
                include_archived=include_archived,
                include_undated=include_undated,
            )
            if entries:
                result[name] = entries
        return result

    def values_in_range(
        self,
        start: datetime | None,
        end: datetime | None,
        *,
        knowledge_as_of: datetime | None = None,
        property_names: tuple[str, ...] = (),
        include_archived: bool = False,
        include_undated: bool = False,
    ) -> dict[str, list[SchemaPropertyEntry]]:
        lower = _aware(start)
        upper = _aware(end)
        result: dict[str, list[SchemaPropertyEntry]] = {}
        for name in self._selected_names(property_names):
            entries = self.timeline(
                name,
                knowledge_as_of=knowledge_as_of,
                include_archived=include_archived,
                include_undated=include_undated,
            )
            ranged = [entry for entry in entries if _event_overlaps(entry, lower, upper)]
            if include_undated:
                ranged.extend(entry for entry in entries if _event_anchor(entry) is None)
            if ranged:
                result[name] = ranged
        return result

    def get_property_at_time(
        self,
        property_name: str,
        event_at: datetime,
        *,
        knowledge_as_of: datetime | None = None,
        include_archived: bool = False,
        include_undated: bool = False,
    ) -> SchemaPropertyEntry | None:
        selected = self.snapshot(
            event_at,
            knowledge_as_of=knowledge_as_of,
            property_names=(property_name,),
            include_archived=include_archived,
            include_undated=include_undated,
        )
        return selected.get(property_name)

    def get_property_in_range(
        self,
        property_name: str,
        start: datetime | None,
        end: datetime | None,
        *,
        knowledge_as_of: datetime | None = None,
        include_archived: bool = False,
        include_undated: bool = False,
    ) -> SchemaPropertyEntry | None:
        ranged = self.values_in_range(
            start,
            end,
            knowledge_as_of=knowledge_as_of,
            property_names=(property_name,),
            include_archived=include_archived,
            include_undated=include_undated,
        )
        entries = ranged.get(property_name, [])
        return max(entries, key=_entry_sort_key) if entries else None

    def get_property_history(
        self,
        property_name: str,
        *,
        knowledge_as_of: datetime | None = None,
        include_archived: bool = False,
        include_undated: bool = True,
    ) -> list[SchemaPropertyEntry]:
        return self.timeline(
            property_name,
            knowledge_as_of=knowledge_as_of,
            include_archived=include_archived,
            include_undated=include_undated,
        )

    def get_timeline(self, property_name: str | None = None) -> list[datetime]:
        if property_name is None:
            entries = [item for timeline in self.properties.values() for item in timeline]
        else:
            entries = self.properties.get(property_name, [])
        anchors: set[datetime] = set()
        for entry in entries:
            anchor = _event_anchor(entry)
            if anchor is not None:
                anchors.add(anchor)
        return sorted(anchors)

    def get_time_range(self) -> tuple[datetime | None, datetime | None]:
        entries = [item for timeline in self.properties.values() for item in timeline]
        starts: list[datetime] = []
        ends: list[datetime] = []
        for entry in entries:
            anchor = _event_anchor(entry)
            if anchor is None:
                continue
            starts.append(anchor)
            ends.append(entry.event_end or anchor)
        if not starts:
            return None, None
        return min(starts), max(ends)

    def select(
        self,
        query: SchemaTemporalQuery,
    ) -> dict[str, SchemaPropertyEntry] | dict[str, list[SchemaPropertyEntry]]:
        common = {
            "knowledge_as_of": query.knowledge_as_of,
            "property_names": query.property_names,
            "include_archived": query.include_archived,
            "include_undated": bool(query.include_undated),
        }
        if query.mode is SchemaTemporalMode.LATEST:
            return self.latest(**common)
        if query.mode is SchemaTemporalMode.SNAPSHOT:
            if query.event_at is None:
                return {}
            return self.snapshot(query.event_at, **common)
        if query.mode is SchemaTemporalMode.RANGE:
            return self.values_in_range(query.event_from, query.event_to, **common)
        return self.history(**common)

    def knowledge_visible(self, query: SchemaTemporalQuery) -> TemporalEntity:
        """Return a copy filtered only on lifecycle and knowledge-time."""

        properties = self.history(
            knowledge_as_of=query.knowledge_as_of,
            property_names=query.property_names,
            include_archived=query.include_archived,
            include_undated=True,
        )
        return self._copy(properties=properties)

    def event_filtered(self, query: SchemaTemporalQuery) -> TemporalEntity:
        """Return a copy whose timelines match the requested event-time view."""

        known = self.knowledge_visible(query)
        properties = _selected_to_timelines(known.select(query))
        fallback_used = False
        fallback_modes = {SchemaTemporalMode.SNAPSHOT, SchemaTemporalMode.RANGE}
        if not properties and query.fallback_policy is TemporalFallbackPolicy.FULL_TIMELINE:
            if query.mode in fallback_modes:
                properties = known.properties
                fallback_used = bool(properties)
        return known._copy(properties=properties, fallback_used=fallback_used)

    def selected_payload(
        self,
        mode: SchemaTemporalMode,
    ) -> dict[str, SchemaPropertyEntry] | dict[str, list[SchemaPropertyEntry]]:
        """Expose this already-filtered view in the requested result shape."""

        if mode in {SchemaTemporalMode.LATEST, SchemaTemporalMode.SNAPSHOT}:
            return {
                name: max(timeline, key=_entry_sort_key)
                for name, timeline in self.properties.items()
                if timeline
            }
        return {name: list(timeline) for name, timeline in self.properties.items() if timeline}

    def visible(self, query: SchemaTemporalQuery) -> TemporalEntity:
        """Backward-compatible alias for the fully event-filtered view."""

        return self.event_filtered(query)

    def filter_by_time(
        self,
        start: datetime | None,
        end: datetime | None,
        *,
        knowledge_as_of: datetime | None = None,
        include_archived: bool = False,
        include_undated: bool = False,
        fallback_policy: TemporalFallbackPolicy = TemporalFallbackPolicy.NONE,
    ) -> TemporalEntity:
        """Return the event interval view, optionally falling back to history."""

        properties = self.values_in_range(
            start,
            end,
            knowledge_as_of=knowledge_as_of,
            include_archived=include_archived,
            include_undated=include_undated,
        )
        fallback_used = False
        if not properties and fallback_policy is TemporalFallbackPolicy.FULL_TIMELINE:
            properties = self.history(
                knowledge_as_of=knowledge_as_of,
                include_archived=include_archived,
                include_undated=include_undated,
            )
            fallback_used = bool(properties)
        return self._copy(properties=properties, fallback_used=fallback_used)

    def filter_by_timepoints(
        self,
        timepoints: list[datetime],
        *,
        knowledge_as_of: datetime | None = None,
        include_archived: bool = False,
    ) -> TemporalEntity:
        """Return facts whose structured event interval contains any timepoint."""

        targets = [_aware(value) for value in timepoints]
        properties: dict[str, list[SchemaPropertyEntry]] = {}
        for name in self.properties:
            matches: list[SchemaPropertyEntry] = []
            entries = self.timeline(
                name,
                knowledge_as_of=knowledge_as_of,
                include_archived=include_archived,
                include_undated=False,
            )
            for entry in entries:
                if any(target is not None and _event_contains(entry, target) for target in targets):
                    matches.append(entry)
            if matches:
                properties[name] = matches
        return self._copy(properties=properties)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "entity_type": self.entity_type,
            "description": self.description,
            "aliases": list(self.aliases),
            "search_fields": list(self.search_fields),
            "properties": {
                name: [entry.to_dict() for entry in timeline]
                for name, timeline in self.properties.items()
            },
            "fallback_used": self.fallback_used,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TemporalEntity:
        entity = cls(
            entity_id=str(data.get("entity_id") or ""),
            name=str(data.get("name") or ""),
            entity_type=str(data.get("entity_type") or ""),
            description=str(data.get("description") or ""),
            aliases=[str(item) for item in data.get("aliases", [])],
            search_fields=[str(item) for item in data.get("search_fields", [])],
            fallback_used=bool(data.get("fallback_used", False)),
            truncated=bool(data.get("truncated", False)),
        )
        raw_properties = data.get("properties")
        if not isinstance(raw_properties, dict):
            return entity
        for timeline in raw_properties.values():
            if not isinstance(timeline, list):
                continue
            for item in timeline:
                if isinstance(item, dict):
                    entity.add(SchemaPropertyEntry.from_dict(item))
        return entity

    def _copy(
        self,
        *,
        properties: dict[str, list[SchemaPropertyEntry]] | None = None,
        fallback_used: bool | None = None,
    ) -> TemporalEntity:
        selected = properties if properties is not None else self.properties
        return TemporalEntity(
            entity_id=self.entity_id,
            name=self.name,
            entity_type=self.entity_type,
            description=self.description,
            aliases=list(self.aliases),
            search_fields=list(self.search_fields),
            properties={name: list(timeline) for name, timeline in selected.items()},
            fallback_used=self.fallback_used if fallback_used is None else fallback_used,
            truncated=self.truncated,
        )

    def _selected_names(self, requested: tuple[str, ...]) -> list[str]:
        if requested:
            return [name for name in requested if name in self.properties]
        return list(self.properties)


@dataclass(slots=True)
class SchemaTemporalResult:
    """Temporal entity views plus late-fusion data for real MemoryUnit projection."""

    mode: SchemaTemporalMode
    entities: list[TemporalEntity] = field(default_factory=list)
    selected: dict[
        str,
        dict[str, SchemaPropertyEntry] | dict[str, list[SchemaPropertyEntry]],
    ] = field(default_factory=dict)
    fallback_entity_ids: list[str] = field(default_factory=list)
    formatted_context: str = ""
    entity_scores: dict[str, float] = field(default_factory=dict)
    direct_property_unit_ids: dict[str, list[str]] = field(default_factory=dict)
    direct_property_scores: dict[str, float] = field(default_factory=dict)
    shrink_diagnostics: SchemaShrinkDiagnostics | None = None


def schema_entity_key(entity_type: str, entity_name: str) -> str:
    """Return the deterministic fallback identity for legacy schema properties."""

    return schema_entity_fallback_key(entity_type, entity_name)


def _unit_entity_id(unit: MemoryUnit) -> str:
    return schema_property_entity_id(unit)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _knowledge_time(entry: SchemaPropertyEntry) -> datetime:
    return (
        entry.valid_from
        or entry.ingest_time
        or entry.message_time
        or datetime.min.replace(tzinfo=timezone.utc)
    )


def _entry_sort_key(entry: SchemaPropertyEntry) -> tuple[int, datetime, datetime, str]:
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    anchor = _event_anchor(entry)
    return (
        1 if anchor is not None else 0,
        anchor or epoch,
        _knowledge_time(entry),
        entry.unit_id,
    )


def _latest_sort_key(entry: SchemaPropertyEntry) -> tuple[datetime, datetime, str]:
    return (
        _event_anchor(entry) or _knowledge_time(entry),
        _knowledge_time(entry),
        entry.unit_id,
    )


def _event_anchor(entry: SchemaPropertyEntry) -> datetime | None:
    return entry.event_start or entry.event_time


def _event_overlaps(
    entry: SchemaPropertyEntry,
    lower: datetime | None,
    upper: datetime | None,
) -> bool:
    start = _event_anchor(entry)
    if start is None:
        return False
    end = entry.event_end
    if lower is not None:
        if end is not None and end <= lower:
            return False
        if end is None and start < lower:
            return False
    return upper is None or start < upper


def _event_contains(entry: SchemaPropertyEntry, target: datetime) -> bool:
    start = _event_anchor(entry)
    if start is None or start > target:
        return False
    return entry.event_end is None or target < entry.event_end


def _selected_to_timelines(
    selected: dict[str, SchemaPropertyEntry] | dict[str, list[SchemaPropertyEntry]],
) -> dict[str, list[SchemaPropertyEntry]]:
    result: dict[str, list[SchemaPropertyEntry]] = {}
    for name, value in selected.items():
        result[name] = list(value) if isinstance(value, list) else [value]
    return result


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}", text):
        text = f"{text}-01-01T00:00:00+00:00"
    elif re.fullmatch(r"\d{4}-\d{2}", text):
        text = f"{text}-01T00:00:00+00:00"
    normalized = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        return _aware(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def _event_precision(value: object) -> EventTimePrecision:
    try:
        return _enum_value(EventTimePrecision, value)
    except ValueError:
        return EventTimePrecision.UNKNOWN


def _enum_value(enum_type: type[Enum], value: object) -> Any:
    if isinstance(value, enum_type):
        return value
    return enum_type(str(value))


def _lifecycle(value: object) -> LifecycleState:
    try:
        return LifecycleState(str(value or LifecycleState.ACTIVE.value))
    except ValueError:
        return LifecycleState.ACTIVE


def _exclusive_end(
    start: datetime | None,
    precision: EventTimePrecision,
) -> datetime | None:
    if start is None:
        return None
    if precision is EventTimePrecision.YEAR:
        return start.replace(year=start.year + 1)
    if precision is EventTimePrecision.MONTH:
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1)
        return start.replace(month=start.month + 1)
    if precision is EventTimePrecision.DAY:
        return start + timedelta(days=1)
    if precision is EventTimePrecision.DATETIME:
        return start + timedelta(microseconds=1)
    return None


def _known_at(
    entry: SchemaPropertyEntry,
    knowledge_as_of: datetime | None,
    include_archived: bool,
) -> bool:
    if entry.lifecycle is LifecycleState.FORGOTTEN:
        return False
    if knowledge_as_of is None:
        if entry.lifecycle is LifecycleState.ACTIVE:
            now = datetime.now(timezone.utc)
            starts_before_now = entry.valid_from is None or entry.valid_from <= now
            ends_after_now = entry.valid_to is None or entry.valid_to > now
            return starts_before_now and ends_after_now
        return include_archived and entry.lifecycle in {
            LifecycleState.ARCHIVED,
            LifecycleState.SUPERSEDED,
        }
    target = _aware(knowledge_as_of)
    if target is None:
        return False
    starts_before_target = entry.valid_from is None or entry.valid_from <= target
    ends_after_target = entry.valid_to is None or target < entry.valid_to
    return starts_before_target and ends_after_target


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _coerce_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)
