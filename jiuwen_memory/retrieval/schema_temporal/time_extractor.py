# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deterministic and optional LLM extraction of event-time query windows."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from jiuwen_memory.common.llm.base import LLM

from .model import EventTimePrecision

logger = logging.getLogger(__name__)

_ENGLISH_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_ENGLISH_MONTH_PATTERN = "|".join(
    sorted((re.escape(name) for name in _ENGLISH_MONTHS), key=len, reverse=True)
)

_TIME_EXTRACTION_PROMPT = """You are a time extraction expert. Analyze the user's query and
extract an event-time constraint that can narrow the schema-memory search window.

User query: {query}
Current UTC time (for resolving relative expressions): {current_time}

Return exactly one JSON object with exactly these fields:
{{"has_time": boolean, "start": "ISO-8601-or-empty", "end": "ISO-8601-or-empty",
  "precision": "unknown|year|month|day|datetime"}}

Rules:
1. Resolve explicit years, months, dates, datetimes, and unambiguous relative expressions against
   current UTC time.
2. start/end define a UTC half-open interval [start, end). A year covers Jan 1 through the next
   Jan 1; a month covers its first day through the next month; a day ends at the next day.
3. Preserve the precision expressed by the query. Do not invent a day for a year/month query.
4. If the query contains no usable event-time constraint, set has_time=false and both bounds empty.
5. Never invent a time. When uncertain, return no constraint so retrieval can proceed safely.
6. Output JSON only, without markdown or commentary.
"""


class TemporalIntentSource(str, Enum):
    """Origin of a detected temporal constraint."""

    NONE = "none"
    EXPLICIT = "explicit"
    RULE = "rule"
    LLM = "llm"


@dataclass(frozen=True, slots=True)
class ExtractedTimeWindow:
    """UTC half-open event-time interval with the user's original precision."""

    start: datetime | None = None
    end: datetime | None = None
    precision: EventTimePrecision = EventTimePrecision.UNKNOWN
    source: TemporalIntentSource = TemporalIntentSource.NONE

    @property
    def found(self) -> bool:
        return self.start is not None or self.end is not None


class SchemaTimeExtractor:
    """Extract a UTC half-open event-time window from natural language."""

    def __init__(
        self,
        llm: LLM | None = None,
        *,
        llm_enabled: bool = False,
        min_window_days: float = 0.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._llm = llm
        self._llm_enabled = bool(llm_enabled)
        self._min_window = max(0.0, float(min_window_days))
        self._now = now or (lambda: datetime.now(timezone.utc))

    def extract(self, text: str) -> ExtractedTimeWindow:
        now = _aware(self._now())
        result = _rule_based(str(text or ""), now)
        if not result.found and self._llm_enabled and self._llm is not None:
            result = self._extract_with_llm(text, now)
        return self._enforce_min_window(result)

    def _extract_with_llm(self, text: str, now: datetime) -> ExtractedTimeWindow:
        prompt = _TIME_EXTRACTION_PROMPT.format(query=text, current_time=now.isoformat())
        try:
            raw = self._llm.generate(prompt, temperature=0)
            data = json.loads(raw)
            expected = {"has_time", "start", "end", "precision"}
            if not isinstance(data, dict) or set(data) != expected:
                return ExtractedTimeWindow()
            if data["has_time"] is not True:
                return ExtractedTimeWindow()
            start = _parse_datetime(data["start"])
            end = _parse_datetime(data["end"])
            if start is None and end is None:
                return ExtractedTimeWindow()
            if start is not None and end is not None and start >= end:
                return ExtractedTimeWindow()
            precision = EventTimePrecision(str(data["precision"]))
            return ExtractedTimeWindow(start, end, precision, TemporalIntentSource.LLM)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("schema temporal LLM time extraction rejected: %s", exc)
            return ExtractedTimeWindow()
        except Exception as exc:
            logger.warning(
                "schema temporal LLM time extraction failed: %s",
                exc,
                exc_info=True,
            )
            return ExtractedTimeWindow()

    def _enforce_min_window(self, result: ExtractedTimeWindow) -> ExtractedTimeWindow:
        if not result.found or self._min_window <= 0:
            return result
        if result.start is None or result.end is None:
            return result
        minimum = timedelta(days=self._min_window)
        if result.end - result.start >= minimum:
            return result
        midpoint = result.start + (result.end - result.start) / 2
        return ExtractedTimeWindow(
            midpoint - minimum / 2,
            midpoint + minimum / 2,
            result.precision,
            result.source,
        )


def _rule_based(text: str, now: datetime) -> ExtractedTimeWindow:
    absolute = _absolute_range(text)
    if absolute.found:
        return absolute
    relative = _relative_range(text, now)
    if relative is None:
        return ExtractedTimeWindow()
    start, end, precision = relative
    return ExtractedTimeWindow(start, end, precision, TemporalIntentSource.RULE)


def _absolute_range(text: str) -> ExtractedTimeWindow:
    range_match = re.search(
        r"(?P<sy>\d{4})\s*年(?:\s*(?P<sm>\d{1,2})\s*月)?"
        r"\s*(?:至|到|—|-|~|～)\s*"
        r"(?P<ey>\d{4})\s*年(?:\s*(?P<em>\d{1,2})\s*月)?",
        text,
    )
    if range_match:
        start = _month_or_year_start(range_match.group("sy"), range_match.group("sm"))
        end_start = _month_or_year_start(range_match.group("ey"), range_match.group("em"))
        end = _next_month(end_start) if range_match.group("em") else _next_year(end_start)
        precision = EventTimePrecision.YEAR
        if range_match.group("sm") or range_match.group("em"):
            precision = EventTimePrecision.MONTH
        return ExtractedTimeWindow(start, end, precision, TemporalIntentSource.RULE)

    iso_range = re.search(
        r"(?P<start>\d{4}-\d{2}(?:-\d{2})?)\s*(?:至|到|—|~|～)\s*"
        r"(?P<end>\d{4}-\d{2}(?:-\d{2})?)",
        text,
    )
    if iso_range:
        start, start_precision = _parse_partial_date(iso_range.group("start"))
        end_start, end_precision = _parse_partial_date(iso_range.group("end"))
        if start is not None and end_start is not None:
            precision = EventTimePrecision.UNKNOWN
            if start_precision == end_precision:
                precision = start_precision
            return ExtractedTimeWindow(
                start,
                _exclusive_end(end_start, end_precision),
                precision,
                TemporalIntentSource.RULE,
            )

    chinese = re.search(
        r"(?<!\d)(?P<year>\d{4})\s*年"
        r"(?:\s*(?P<month>\d{1,2})\s*月)?"
        r"(?:\s*(?P<day>\d{1,2})\s*(?:日|号))?",
        text,
    )
    if chinese:
        try:
            start, precision = _date_parts(
                int(chinese.group("year")),
                int(chinese.group("month")) if chinese.group("month") else None,
                int(chinese.group("day")) if chinese.group("day") else None,
            )
        except ValueError:
            return ExtractedTimeWindow()
        return ExtractedTimeWindow(
            start,
            _exclusive_end(start, precision),
            precision,
            TemporalIntentSource.RULE,
        )

    iso = re.search(r"(?<!\d)(\d{4}-\d{2}(?:-\d{2})?)(?!\d)", text)
    if iso:
        start, precision = _parse_partial_date(iso.group(1))
        if start is not None:
            return ExtractedTimeWindow(
                start,
                _exclusive_end(start, precision),
                precision,
                TemporalIntentSource.RULE,
            )

    english = _english_absolute_date(text)
    if english.found:
        return english

    year = re.search(r"(?<![\d-])((?:19|20|21)\d{2})(?![\d-])", text)
    if year:
        start = datetime(int(year.group(1)), 1, 1, tzinfo=timezone.utc)
        return ExtractedTimeWindow(
            start,
            _next_year(start),
            EventTimePrecision.YEAR,
            TemporalIntentSource.RULE,
        )
    return ExtractedTimeWindow()


def _relative_range(
    text: str,
    now: datetime,
) -> tuple[datetime, datetime, EventTimePrecision] | None:
    match = re.search(
        r"(?:最近|过去|近)\s*(\d+)\s*(天|日|周|星期)|"
        r"\b(?:last|past)\s+(\d+)\s+(days?|weeks?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        amount = int(match.group(1) or match.group(3))
        unit = (match.group(2) or match.group(4)).casefold()
        week_units = {"周", "星期", "week", "weeks"}
        delta = timedelta(weeks=amount) if unit in week_units else timedelta(days=amount)
        return now - delta, now, EventTimePrecision.DATETIME

    lowered = text.casefold()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "前天" in text or "day before yesterday" in lowered:
        start = day_start - timedelta(days=2)
        return start, start + timedelta(days=1), EventTimePrecision.DAY
    if "昨天" in text or "昨日" in text or "yesterday" in lowered:
        start = day_start - timedelta(days=1)
        return start, start + timedelta(days=1), EventTimePrecision.DAY
    if "今天" in text or "今日" in text or re.search(r"\btoday\b", lowered):
        return day_start, day_start + timedelta(days=1), EventTimePrecision.DAY

    week_start = day_start - timedelta(days=day_start.weekday())
    if "上周" in text or "上星期" in text or "last week" in lowered:
        return week_start - timedelta(days=7), week_start, EventTimePrecision.DAY
    if "本周" in text or "这周" in text or "this week" in lowered:
        return week_start, now, EventTimePrecision.DATETIME

    month_start = day_start.replace(day=1)
    if "上个月" in text or "上月" in text or "last month" in lowered:
        previous = month_start - timedelta(days=1)
        return previous.replace(day=1), month_start, EventTimePrecision.MONTH
    if "下个月" in text or "下月" in text or "next month" in lowered:
        start = _next_month(month_start)
        return start, _next_month(start), EventTimePrecision.MONTH
    if "本月" in text or "这个月" in text or "this month" in lowered:
        return month_start, now, EventTimePrecision.DATETIME

    year_start = month_start.replace(month=1)
    if "去年" in text or "last year" in lowered:
        return _next_year(year_start, -1), year_start, EventTimePrecision.YEAR
    if "明年" in text or "next year" in lowered:
        start = _next_year(year_start)
        return start, _next_year(start), EventTimePrecision.YEAR
    if "今年" in text or "this year" in lowered:
        return year_start, now, EventTimePrecision.DATETIME
    return None


def _english_absolute_date(text: str) -> ExtractedTimeWindow:
    patterns = (
        (
            rf"\b(?P<month>{_ENGLISH_MONTH_PATTERN})\.?\s+"
            r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:\s*,)?\s+"
            r"(?P<year>\d{4})\b"
        ),
        (
            r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+"
            rf"(?P<month>{_ENGLISH_MONTH_PATTERN})\.?(?:\s*,)?\s+"
            r"(?P<year>\d{4})\b"
        ),
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match is None:
            continue
        try:
            start, precision = _date_parts(
                int(match.group("year")),
                _ENGLISH_MONTHS[match.group("month").casefold()],
                int(match.group("day")),
            )
        except (KeyError, ValueError):
            return ExtractedTimeWindow()
        return ExtractedTimeWindow(
            start,
            _exclusive_end(start, precision),
            precision,
            TemporalIntentSource.RULE,
        )

    month = re.search(
        rf"\b(?P<month>{_ENGLISH_MONTH_PATTERN})\.?\s+(?P<year>\d{{4}})\b",
        text,
        flags=re.IGNORECASE,
    )
    if month is None:
        return ExtractedTimeWindow()
    try:
        start, precision = _date_parts(
            int(month.group("year")),
            _ENGLISH_MONTHS[month.group("month").casefold()],
            None,
        )
    except (KeyError, ValueError):
        return ExtractedTimeWindow()
    return ExtractedTimeWindow(
        start,
        _exclusive_end(start, precision),
        precision,
        TemporalIntentSource.RULE,
    )


def _date_parts(
    year: int,
    month: int | None,
    day: int | None,
) -> tuple[datetime, EventTimePrecision]:
    if day is not None:
        return datetime(year, month or 1, day, tzinfo=timezone.utc), EventTimePrecision.DAY
    if month is not None:
        return datetime(year, month, 1, tzinfo=timezone.utc), EventTimePrecision.MONTH
    return datetime(year, 1, 1, tzinfo=timezone.utc), EventTimePrecision.YEAR


def _month_or_year_start(year: str, month: str | None) -> datetime:
    return datetime(int(year), int(month or 1), 1, tzinfo=timezone.utc)


def _parse_partial_date(value: str) -> tuple[datetime | None, EventTimePrecision]:
    try:
        if len(value) == 7:
            parsed = datetime.strptime(value, "%Y-%m").replace(tzinfo=timezone.utc)
            return parsed, EventTimePrecision.MONTH
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return parsed, EventTimePrecision.DAY
    except ValueError:
        return None, EventTimePrecision.UNKNOWN


def _exclusive_end(start: datetime, precision: EventTimePrecision) -> datetime:
    if precision is EventTimePrecision.YEAR:
        return _next_year(start)
    if precision is EventTimePrecision.MONTH:
        return _next_month(start)
    if precision is EventTimePrecision.DAY:
        return start + timedelta(days=1)
    return start + timedelta(microseconds=1)


def _next_year(value: datetime, amount: int = 1) -> datetime:
    return value.replace(year=value.year + amount)


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        return _aware(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
