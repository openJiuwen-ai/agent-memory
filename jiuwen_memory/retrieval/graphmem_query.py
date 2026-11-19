"""Soft evidence needs; never authorization predicates or an answer-correctness proof."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import timedelta

from jiuwen_memory.common.graphmem_semantics import canonical, iso_time, read_semantics
from jiuwen_memory.common.type_def import HierarchyKind, ParsedQuery

MODES = ("fact", "collection", "temporal", "state", "comparison", "multi_step")
_UNNAMED_REFERENTS = frozenset((
    "i me my mine myself we us our ours ourselves you your yours yourself yourselves "
    "he him his himself she her hers herself they them their theirs themselves "
    "user assistant"
).split()) | {"我", "我们", "你", "你们", "他", "她", "他们", "她们", "用户", "助手"}


def is_named_entity(value):
    """A pronoun/role does not identify a stored person; keep named phrases intact."""
    return bool(canonical(value) and canonical(value) not in _UNNAMED_REFERENTS)


_STOP = frozenset((
    "what when where which who why how did does do is was were are the a an and or of to "
    "in on for his her their before after i me my you your have has had been can could "
    "would should about tell please session summary user assistant that this from with "
    "all list many much latest current say said mention mentioned according information"
).split())


def terms(text):
    words = set(re.findall(r"[\w]+", canonical(text))) - _STOP
    return {w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w
            for w in words}


@dataclass(frozen=True)
class QueryNeeds:
    mode: str = "fact"
    entities: tuple[str, ...] = ()
    facets: tuple[str, ...] = ()
    time_from: str = ""
    time_to: str = ""
    analysis_status: str = "rules"
    model_calls: int = 0


@dataclass
class GraphMemParsedQuery(ParsedQuery):
    # A typed extension survives normal dataclass replacement and does not share
    # the caller-controlled extensions namespace used by authorization/routing.
    navigation_needs: QueryNeeds = field(default_factory=QueryNeeds)


def rule_needs(text, *, status="rules", model_calls=0):
    patterns = (
        ("collection", r"\b(all|list|how many|total|every|each)\b|哪些|多少|所有|列出"),
        ("comparison", r"\b(compare|comparison|difference|both)\b|比较|区别|分别"),
        ("state", r"\b(latest|current|currently|change|changed|still|no longer)\b|目前|变化|最新"),
        ("temporal", r"\b(when|before|after|between|earlier|later|how long)\b|何时|之前|之后|多久"),
    )
    mode = next((name for name, pattern in patterns if re.search(pattern, text.casefold())), "fact")
    return QueryNeeds(mode=mode, facets=(text,), analysis_status=status, model_calls=model_calls)


def needs_for(query):
    return getattr(query, "navigation_needs", None) or rule_needs(query.rewritten or query.raw)


def conversation_day_span(query: ParsedQuery):
    """A narrow soft routing predicate for questions about the conversation itself."""
    needs = needs_for(query)
    if (query.hierarchy_kind is not HierarchyKind.TIME
            or needs.mode != "temporal" or len(needs.facets) != 1
            or needs.analysis_status not in {"used", "used_rules_guard"}
            or not (needs.time_from and needs.time_to)):
        return None
    if not re.match(r"^\s*(?:what|which)\b", query.raw, re.I):
        return None
    match = re.search(
        r"\b(?:topic of (?:the )?discussion|topic of (?:the )?conversation|"
        r"did [^?]{1,100}? (?:talk about|discuss))\s+(?:between [^?]{1,80}?\s+)?on\s+([^?]+)",
        query.raw, re.I,
    )
    if match is None:
        return None
    start, stop = iso_time(needs.time_from), iso_time(needs.time_to)
    if (start.hour or start.minute or start.second or start.microsecond
            or stop - start != timedelta(days=1)):
        return None
    months = ("january", "february", "march", "april", "may", "june", "july",
              "august", "september", "october", "november", "december")
    month = months[start.month - 1]
    literal = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "",
                     match[1].strip(" .").casefold().replace(",", ""))
    literal = " ".join(literal.split())
    day, year = start.day, start.year
    if literal not in {start.date().isoformat(),
                       f"{day} {month} {year}", f"{month} {day} {year}",
                       f"{day} {month[:3]} {year}", f"{month[:3]} {day} {year}"}:
        return None
    return start, stop - timedelta(microseconds=1)


def entry_keyword_query(query: ParsedQuery) -> ParsedQuery:
    """Add missing topic vocabulary to summary recall using the cached query analysis."""
    needs = needs_for(query)
    if needs.analysis_status not in {"used", "used_rules_guard"}:
        return query
    text = query.rewritten or query.raw
    known = terms(text)
    additions = []
    for facet in needs.facets:
        words = terms(facet)
        if words - known:
            additions.append(facet)
            known.update(words)
    return replace(query, rewritten=" ".join([text, *additions])) if additions else query


def facet_terms(needs):
    entity_words = set().union(*(terms(e) for e in needs.entities)) if needs.entities else set()
    return [terms(facet) - entity_words for facet in needs.facets]


def facet_coverage(text, needs):
    words = terms(text)
    return [len(wanted & words) / len(wanted) if wanted else 0.0
            for wanted in facet_terms(needs)]


def relevance(unit, needs):
    """Prefer the query's scenes and attributed times without excluding unknown dates."""
    features = read_semantics(unit)
    text = unit.layers.l1 or unit.content
    score = max(facet_coverage(text, needs), default=0.0)
    present = canonical(text)
    if features:
        present += " " + " ".join(canonical(n) for e in features.entities
                                   for n in (e.name, *e.aliases))
    score += 0.25 * sum(canonical(e) in present for e in needs.entities)
    if features and (needs.time_from or needs.time_to):
        start = iso_time(needs.time_from) if needs.time_from else None
        end = iso_time(needs.time_to) if needs.time_to else None
        if any(e.time.known and (start is None or iso_time(e.time.end) > start)
               and (end is None or iso_time(e.time.start) < end) for e in features.events):
            score += 0.5
    return score
