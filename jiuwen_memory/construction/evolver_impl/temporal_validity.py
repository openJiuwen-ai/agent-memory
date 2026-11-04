# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Conservative time guards for modification decisions, independent of stores and LLMs.

Message time orders statements, not business validity. Never backdate t_valid from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from jiuwen_memory.common.type_def import MemoryUnit


def aware_utc(value: datetime | None) -> datetime | None:
    """Unknown timezones are unknown times; attaching UTC would invent information."""
    if value is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


@dataclass(frozen=True)
class TemporalVerdict:
    action: str  # newer / older / preserve
    reason: str
    boundary: datetime | None = None


def compare_validity(
    candidate: MemoryUnit, existing: MemoryUnit, *, now: datetime
) -> TemporalVerdict:
    """Guard an already identified UPDATE/SUPERSEDE relationship.

    t_event is deliberately excluded: distinct events and changed event dates cannot
    be distinguished from timestamps alone. Missing/naive/equal times preserve both.
    Message times later than now are also preserved, with no implicit clock-skew
    allowance. The caller downgrades UPDATE/SUPERSEDE to ADD for preserve verdicts.
    """
    for unit in (candidate, existing):
        start = aware_utc(unit.temporal.t_valid)
        end = aware_utc(unit.temporal.t_invalid)
        if any(
            raw is not None and aware_utc(raw) is None
            for raw in (unit.temporal.t_valid, unit.temporal.t_invalid)
        ):
            return TemporalVerdict("preserve", "unknown_validity_timezone")
        if start is not None and end is not None and end <= start:
            return TemporalVerdict("preserve", "invalid_validity_interval")
    c_start = aware_utc(candidate.temporal.t_valid)
    e_start = aware_utc(existing.temporal.t_valid)
    c_end = aware_utc(candidate.temporal.t_invalid)
    e_end = aware_utc(existing.temporal.t_invalid)
    c_event = aware_utc(candidate.temporal.t_event)
    has_future_event = c_event is not None and c_event > now
    has_future_validity = c_start is not None and c_start > now
    if has_future_event and not has_future_validity:
        # A future event alone does not tell us when a state replacement takes effect.
        return TemporalVerdict("preserve", "future_event_requires_validity")
    candidate_ends_before_existing = (
        c_end is not None and e_start is not None and c_end <= e_start
    )
    existing_ends_before_candidate = (
        e_end is not None and c_start is not None and e_end <= c_start
    )
    if candidate_ends_before_existing or existing_ends_before_candidate:
        return TemporalVerdict("preserve", "disjoint_validity")
    c_time = aware_utc(candidate.temporal.t_message)
    e_time = aware_utc(existing.temporal.t_message)
    if c_time is None or e_time is None:
        return TemporalVerdict("preserve", "unknown_message_time")
    if c_time > now or e_time > now:
        return TemporalVerdict("preserve", "future_message_time")
    if c_time == e_time:
        return TemporalVerdict("preserve", "equal_message_time")
    if c_time < e_time:
        # The candidate only became known on ingestion. Closing it at the earlier
        # message time would create an inverted interval for ordinary extracted units.
        if c_start is not None and c_start >= now:
            return TemporalVerdict("preserve", "candidate_not_yet_valid")
        boundary = max(now, e_start) if e_start is not None else now
        if c_end is not None:
            boundary = min(boundary, c_end)
        return TemporalVerdict("older", "older_statement", boundary)
    # Future validity is preserved; the existing unit must stay ACTIVE until then.
    boundary = max(now, c_start) if c_start is not None else now
    if e_start is not None and boundary <= e_start:
        return TemporalVerdict("preserve", "non_positive_existing_interval")
    return TemporalVerdict("newer", "newer_statement", boundary)


def time_context(unit: MemoryUnit) -> str:
    """Label independent axes so the model does not mistake event time for recency."""
    return (
        f"Message time (statement order): {unit.temporal.t_message}\n"
        f"Event time (not statement order): {unit.temporal.t_event}\n"
        f"Validity interval: [{unit.temporal.t_valid}, {unit.temporal.t_invalid})"
    )
