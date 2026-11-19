"""Bounded, attributed entity/scene/event-time features shared by graph consumers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryUnit

FEATURES_KEY = "graphmem_semantics"
FEATURES_VERSION = 1
ENTITY_KINDS = ("person", "organization", "place", "work", "object", "other")
EVENT_STATES = ("planned", "completed", "ongoing", "cancelled", "suggested", "reported", "unknown")
TIME_PRECISIONS = ("instant", "day", "month", "year", "range", "unknown")
TIME_KINDS = ("occurrence", "duration", "uncertain", "recurrence", "unknown")


def canonical(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def stable_id(kind: str, *values) -> str:
    data = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return kind + ":" + hashlib.sha256(data.encode()).hexdigest()[:20]


def iso_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # The source adapter expresses naive conversation dates in UTC explicitly.
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC))


def source_clock(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class EntityLabel:
    name: str
    kind: str
    aliases: tuple[str, ...] = ()
    qualifier: str = ""
    sources: tuple[int, ...] = ()

    @property
    def key(self) -> str:
        return stable_id("entity", self.kind, canonical(self.name), canonical(self.qualifier))

    @property
    def match_keys(self) -> frozenset[str]:
        return frozenset(stable_id("entity", self.kind, canonical(name), canonical(self.qualifier))
                         for name in (self.name, *self.aliases))


@dataclass(frozen=True)
class EventTime:
    start: str = ""
    end: str = ""
    precision: str = "unknown"
    kind: str = "unknown"
    expression: str = ""
    anchor: str = ""
    resolution: str = "unknown"

    @property
    def known(self) -> bool:
        return bool(self.start and self.end and self.precision != "unknown"
                    and self.kind not in ("unknown", "recurrence"))

    @property
    def width(self) -> float:
        return (iso_time(self.end) - iso_time(self.start)).total_seconds() if self.known else 0


@dataclass(frozen=True)
class SceneEvent:
    scene: str
    entity_keys: tuple[str, ...]
    status: str
    time: EventTime
    sources: tuple[int, ...]

    @property
    def key(self) -> str:
        # Keep state transitions and distinct temporal interpretations separate.
        return stable_id("event", canonical(self.scene), sorted(self.entity_keys), self.status,
                         asdict(self.time))


@dataclass(frozen=True)
class GraphSemantics:
    entities: tuple[EntityLabel, ...] = ()
    events: tuple[SceneEvent, ...] = ()

    def projection(self) -> dict:
        return {
            "version": FEATURES_VERSION,
            "entities": [{**asdict(e), "id": e.key} for e in self.entities],
            "events": [{**asdict(e), "id": e.key} for e in self.events],
        }

    def encode(self) -> str:
        return json.dumps(self.projection(), ensure_ascii=False, separators=(",", ":"))

    def model_view(self) -> dict:
        """No descendant/raw-text catalog; sources address the direct input records."""
        names = {e.key: e.name for e in self.entities}
        return {"entities": [{"name": e.name, "kind": e.kind, "aliases": list(e.aliases),
                              "qualifier": e.qualifier} for e in self.entities],
                "events": [{"id": f"E{i}", "scene": e.scene, "status": e.status,
                            "entities": [names[k] for k in e.entity_keys if k in names],
                            "time": None if e.time == EventTime() else asdict(e.time)}
                           for i, e in enumerate(self.events)]}


def read_semantics(unit: MemoryUnit) -> GraphSemantics | None:
    return decode_semantics(unit.system_metadata.get(FEATURES_KEY))


def decode_semantics(raw: str | None) -> GraphSemantics | None:
    """Validate the same stored attributes whether loaded from a unit or its index."""
    if raw is None:
        return None
    try:
        if not isinstance(raw, str) or len(raw) > 131072:
            raise ValueError("invalid feature payload")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != FEATURES_VERSION:
            raise ValueError("unsupported semantics version")
        if len(data["entities"]) > 16 or len(data["events"]) > 16:
            raise ValueError("unbounded graph semantics")
        entities = tuple(EntityLabel(e["name"], e["kind"], tuple(e["aliases"]),
                                     e["qualifier"], tuple(e["sources"])) for e in data["entities"])
        events = tuple(SceneEvent(e["scene"], tuple(e["entity_keys"]), e["status"],
                                  EventTime(**e["time"]), tuple(e["sources"]))
                       for e in data["events"])
        for entity, encoded in zip(entities, data["entities"], strict=True):
            if (not entity.name or entity.kind not in ENTITY_KINDS or len(entity.aliases) > 4
                    or encoded["id"] != entity.key):
                raise ValueError("invalid entity")
        keys = {e.key for e in entities}
        for event, encoded in zip(events, data["events"], strict=True):
            if (not event.scene or event.status not in EVENT_STATES or encoded["id"] != event.key
                    or not set(event.entity_keys) <= keys):
                raise ValueError("invalid event")
            validate_time(event.time)
        return GraphSemantics(entities, events)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValidationError("invalid stored graph semantics") from exc


def validate_time(window: EventTime) -> None:
    if window.precision not in TIME_PRECISIONS or window.kind not in TIME_KINDS:
        raise ValueError("invalid event time type")
    if not window.start and not window.end:
        if window.precision != "unknown":
            raise ValueError("missing event interval")
        return
    if not window.start or not window.end or window.precision == "unknown":
        raise ValueError("partial event interval")
    start, end = iso_time(window.start), iso_time(window.end)
    if start >= end:
        raise ValueError("event interval must be half-open with positive width")
    calendar_zone = source_clock(window.anchor).tzinfo if window.anchor else UTC
    local_start, local_end = start.astimezone(calendar_zone), end.astimezone(calendar_zone)
    if window.precision == "day" and (local_start.time().isoformat() != "00:00:00"
                                     or local_end - local_start != timedelta(days=1)):
        raise ValueError("day precision must cover exactly one calendar day")
    if window.precision in ("month", "year"):
        if local_start.day != 1 or local_start.time().isoformat() != "00:00:00":
            raise ValueError("invalid calendar interval start")
        if window.precision == "month":
            expected = local_start.replace(year=local_start.year + local_start.month // 12,
                                           month=local_start.month % 12 + 1)
        else:
            if local_start.month != 1:
                raise ValueError("invalid calendar year start")
            expected = local_start.replace(year=local_start.year + 1)
        if local_end != expected:
            raise ValueError("invalid calendar interval end")


def interval_gap(left: EventTime, right: EventTime) -> float:
    if not left.known or not right.known:
        return float("inf")
    return max(0.0, (iso_time(left.start) - iso_time(right.end)).total_seconds(),
               (iso_time(right.start) - iso_time(left.end)).total_seconds())


def resolved_relative_day(expression: str, anchor: str) -> datetime | None:
    """Validate common relative-day resolutions against the SOURCE date, never now()."""
    if not anchor:
        return None
    day = source_clock(anchor).replace(hour=0, minute=0, second=0, microsecond=0)
    text = canonical(expression).strip(" .,")
    offset = {"today": 0, "yesterday": -1, "tomorrow": 1,
              "今天": 0, "昨天": -1, "明天": 1, "前天": -2, "后天": 2}.get(text)
    if offset is not None:
        return (day + timedelta(days=offset)).astimezone(UTC)
    numbers = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
               "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a": 1}
    match = re.fullmatch(r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|a) "
                         r"(day|week)s? (ago|earlier|later)", text)
    if match:
        word, unit, direction = match.groups()
        amount = int(word) if word.isdigit() else numbers[word]
        return (day + timedelta(days=amount * (7 if unit == "week" else 1)
                                * (-1 if direction in ("ago", "earlier") else 1))).astimezone(UTC)
    weekdays = "monday tuesday wednesday thursday friday saturday sunday".split()
    match = re.fullmatch(r"(last|next) (" + "|".join(weekdays) + ")", text)
    if match:
        direction, name = match.groups()
        delta = (day.weekday() - weekdays.index(name)) % 7 or 7
        if direction == "next":
            delta = -((weekdays.index(name) - day.weekday()) % 7 or 7)
        return (day - timedelta(days=delta)).astimezone(UTC)
    return None
