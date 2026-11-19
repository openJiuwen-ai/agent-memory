"""Compile bounded query needs to inspectable selection/expansion/join operations."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.graphmem_semantics import (
    canonical,
    decode_semantics,
    iso_time,
    read_semantics,
)
from jiuwen_memory.common.graphmem_structure import is_recursive, read_child_details
from jiuwen_memory.common.type_def import RecallChannel, RecallResult, Scope
from jiuwen_memory.retrieval.graphmem_query import (
    facet_terms,
    is_named_entity,
    needs_for,
    terms,
)


@dataclass(frozen=True)
class RelationProgram:
    mode: str
    entities: tuple[str, ...]
    facets: tuple[str, ...]
    time_from: str
    time_to: str
    relations: tuple[str, ...]
    combine: str
    order: str
    time_relation: str = "adjacent"
    time_anchor: str = ""

    def describe(self):
        return {
            **asdict(self),
            "selections": clause_specs(self),
            "operations": [
                {"op": "select", "clauses": [f"C{i + 1}" for i in range(len(self.facets))]},
                {"op": "expand", "relations": list(self.relations)},
                {"op": self.combine, "via": "selected_relation_paths"},
                {"op": "order", "by": self.order},
            ],
        }


def compile_program(query, needs=None):
    needs = needs or needs_for(query)
    temporal = needs.mode in {
        "temporal",
        "state",
        "comparison",
        "collection",
        "multi_step",
    } or bool(needs.time_from or needs.time_to)
    # Calendar windows already have an explicit interval. For relative EVENT
    # ordering retain the named anchor, rather than equating traversal direction
    # with time direction. Ambiguous/multiple comparisons remain undirected.
    direction, anchor = "adjacent", ""
    matches = list(re.finditer(r"\b(before|after)\b", query.raw, re.I))
    if temporal and not (needs.time_from or needs.time_to) and len(matches) == 1:
        candidate = query.raw[matches[0].end() :].strip(" ?.,")
        if candidate and len(candidate) <= 160 and terms(candidate):
            direction, anchor = matches[0][1].lower(), candidate
    return RelationProgram(
        needs.mode,
        needs.entities,
        needs.facets or (query.raw,),
        needs.time_from,
        needs.time_to,
        ("entity", "semantic", "temporal") if temporal else ("entity", "semantic"),
        "union"
        if needs.mode == "collection"
        else "compare"
        if needs.mode == "comparison"
        else "join"
        if len(needs.facets) > 1
        else "project",
        "event_time_descending"
        if needs.mode == "state"
        else "event_time_ascending"
        if needs.mode == "temporal"
        else "graph_path",
        direction,
        anchor,
    )


def mentions(text, name):
    """Named entities are tokens/phrases, not arbitrary substrings."""
    return bool(
        canonical(name)
        and re.search(r"(?<!\w)" + re.escape(canonical(name)) + r"(?!\w)", canonical(text))
    )


def clause_specs(needs):
    local = [[e for e in needs.entities if mentions(facet, e)] for facet in needs.facets]
    # A name explicitly attached to one side must not become the default for
    # another side. Only names omitted from ALL facet descriptions are shared
    # context (e.g. a subject whose facets are just "birthplace" and "occupation").
    shared = [e for e in needs.entities if not any(e in names for names in local)]
    return [
        {"id": f"C{i + 1}", "scene": facet, "entities": local[i] or shared}
        for i, facet in enumerate(needs.facets)
    ]


def entity_matches(features, names, keys=None):
    if features is None:
        return []
    return [
        entity.name
        for entity in features.entities
        if (keys is None or entity.key in keys)
        and any(
            canonical(name) in {canonical(n) for n in (entity.name, *entity.aliases)}
            for name in names
        )
    ]


def scene_terms(needs):
    wanted = facet_terms(needs)
    # A relation scene describes evidence, while a query may also describe how
    # to combine it. Counting operators are not facts that
    # every supporting event must repeat. Preserve the existing lexical matching
    # for non-counting content: removing grammatical words alone can also remove
    # a shared term without resolving different word forms in the remaining text.
    # This does not add synonyms or relax entity/time binding.
    if needs.mode == "collection":
        operators = {"number", "total", "count", "quantity"}
        wanted = [clause - operators for clause in wanted]
    if needs.time_from or needs.time_to:
        # Dates are an interval predicate, not words that an event's scene title
        # must repeat. Keep numbers for non-calendar questions (e.g. model names).
        calendar = set(
            (
                "january february march april may june july august september october "
                "november december monday tuesday wednesday thursday friday "
                "saturday sunday"
            ).split()
        )
        wanted = [{w for w in clause if w not in calendar and not w.isdigit()} for clause in wanted]
    return wanted


def event_signals(features, event, needs):
    """Bind a clause to ONE event and its attributed entities, never a text union."""
    words, matches = terms(event.scene), []
    for clause, wanted in zip(clause_specs(needs), scene_terms(needs)):
        score = len(wanted & words) / len(wanted) if wanted else 0
        named = entity_matches(features, clause["entities"], event.entity_keys)
        # An explicitly attributed different participant cannot borrow a name
        # from an unrelated event in the same summary. Missing metadata is unknown.
        if clause["entities"] and event.entity_keys and not named:
            continue
        if score >= 0.5:
            matches.append({"clause": clause["id"], "entities": named, "scene_score": score})
    window = "unknown"
    if event.time.known:
        window = (
            "outside"
            if (
                needs.time_from
                and iso_time(event.time.end) <= iso_time(needs.time_from)
                or needs.time_to
                and iso_time(event.time.start) >= iso_time(needs.time_to)
            )
            else "match"
            if needs.time_from or needs.time_to
            else "unconstrained"
        )
    return {
        "event_id": event.key,
        "scene": event.scene,
        "matches": matches,
        "window": window,
        "interval": [event.time.start, event.time.end],
    }


def ground_query_entities(query, candidates):
    """Resolve explicitly mentioned stored names/aliases without another model call."""
    needs = needs_for(query)
    stored = [
        entity
        for candidate in candidates
        if (features := read_semantics(candidate.unit))
        for entity in features.entities
    ]
    if needs.entities:
        # Keep literal named organizations/works intact. For a single unresolved
        # possessive object, resolve its explicit owner only when the already-read
        # index gives one unambiguous identity. Keep the object in the scene: using
        # just its owner would turn a shop question into every event about them.
        if len(needs.entities) == len(needs.facets) == 1:
            name = needs.entities[0]
            possessive = re.fullmatch(r"(.+?)[’']s\s+(.+)", name, re.I)
            exact = any(
                canonical(name) in {canonical(n) for n in (e.name, *e.aliases)} for e in stored
            )
            if possessive and not exact and mentions(query.raw, name):
                owner, topic = possessive.groups()
                matches = {
                    e.key: e
                    for e in stored
                    if is_named_entity(owner)
                    and canonical(owner) in {canonical(n) for n in (e.name, *e.aliases)}
                }
                if len(matches) == 1:
                    entity = next(iter(matches.values()))
                    facet = needs.facets[0]
                    if not mentions(facet, topic):
                        facet = topic + " " + facet
                    return replace(needs, entities=(entity.name,), facets=(facet,))
        return needs
    found = {}
    text = canonical(query.raw)
    for entity in stored:
        if is_named_entity(entity.name) and any(
            is_named_entity(name) and mentions(text, name)
            for name in (entity.name, *entity.aliases)
        ):
            found.setdefault(canonical(entity.name), entity.name)
    return replace(needs, entities=tuple(list(found.values())[:8]))


def node_signals(unit, needs):
    """Soft topic/entity/time support; no authorization or answer correctness inference."""
    features = read_semantics(unit)
    events = (
        [event_signals(features, event, needs) for event in features.events] if features else []
    )
    hits = [hit for event in events if event["window"] != "outside" for hit in event["matches"]]
    scores = [hit["scene_score"] for hit in hits]
    clauses = list(dict.fromkeys(hit["clause"] for hit in hits))
    # Extracted scene labels can be coarser than the detailed summary. Recover
    # explicit sentence-local support without joining unrelated sentences/names.
    text_hits = {}
    outside = {
        h["clause"] for event in events if event["window"] == "outside" for h in event["matches"]
    } - set(clauses)
    for sentence in re.split(r"[.!?。！？;；\n]+", unit.content or unit.layers.l1 or ""):
        words = terms(sentence)
        for clause, wanted in zip(clause_specs(needs), scene_terms(needs)):
            if clause["id"] in outside:
                continue
            score = len(wanted & words) / len(wanted) if wanted else 0
            named = [e for e in clause["entities"] if mentions(sentence, e)]
            if features:
                named += [
                    e.name
                    for e in features.entities
                    if e.name in entity_matches(features, clause["entities"])
                    and any(mentions(sentence, name) for name in (e.name, *e.aliases))
                ]
            if clause["entities"] and not named:
                continue
            scores.append(score)
            if score >= 0.5:
                if clause["id"] not in clauses:
                    clauses.append(clause["id"])
                text_hits.setdefault(
                    clause["id"],
                    {
                        "clause": clause["id"],
                        "entities": list(dict.fromkeys(named)),
                        "scene_score": score,
                    },
                )
    names = (
        {canonical(n) for e in features.entities for n in (e.name, *e.aliases)}
        if features
        else set()
    )
    text = canonical(unit.layers.l1 or unit.content)
    entities = [e for e in needs.entities if canonical(e) in names or mentions(text, e)]
    times = [
        {"start": event["interval"][0], "end": event["interval"][1]}
        for event in events
        if event["matches"] and event["window"] in {"match", "unconstrained"}
    ]
    return {
        "clauses": clauses,
        "entities": entities,
        "event_intervals": times[:4],
        "event_bindings": [event for event in events if event["matches"]],
        "text_bindings": list(text_hits.values()),
        "scene_score": max(scores, default=0),
        "score": max(scores, default=0)
        + 0.25 * len(entities)
        + 0.25 * bool(times and (needs.time_from or needs.time_to)),
    }


def child_event_hints(parent, needs, child_scope, ids, role):
    """Local event ordinals guide direct-child selection, never authorize a read."""
    if not is_recursive(parent):
        return {}
    features = read_semantics(parent)
    kinds = parent.system_metadata.get("graphmem_child_roles", [])
    if (
        features is None
        or not isinstance(kinds, list)
        or len(kinds) != len(parent.hierarchy.child_ids)
    ):
        return {}
    allowed = set(ids)
    hints, priority = {}, {}
    for event in features.events:
        signal = event_signals(features, event, needs)
        if not signal["matches"] or signal["window"] == "outside":
            continue
        clauses = sorted({h["clause"] for h in signal["matches"]})
        quality = max(h["scene_score"] for h in signal["matches"])
        for position in event.sources:
            if (
                type(position) is not int
                or not 0 <= position < len(kinds)
                or kinds[position] != role.value
                or parent.hierarchy.child_scope_at(position, parent.scope) != child_scope
            ):
                continue
            uid = parent.hierarchy.child_ids[position]
            if uid not in allowed:
                continue
            hints.setdefault(uid, []).append(
                {"event_id": event.key, "source_position": position, "clauses": clauses}
            )
            rank = (len(clauses), quality, signal["window"] == "match", -position)
            priority[uid] = max(priority.get(uid, rank), rank)
    return {uid: hints[uid] for uid in sorted(hints, key=priority.get, reverse=True)}


def child_detail_hints(parent, needs, child_scope, ids, role):
    """Locate a direct child in an already-read summary, without an event claim."""
    # Unstructured text cannot establish a calendar interval. Dated navigation
    # continues to use the existing validated event-reference path.
    if needs.time_from or needs.time_to:
        return {}
    details = read_child_details(parent)
    kinds = parent.system_metadata.get("graphmem_child_roles", [])
    if not isinstance(kinds, list) or len(kinds) != len(parent.hierarchy.child_ids):
        return {}
    allowed, hints, priority = set(ids), {}, {}
    for position, text in details.items():
        uid = parent.hierarchy.child_ids[position]
        if (
            uid not in allowed
            or kinds[position] != role.value
            or parent.hierarchy.child_scope_at(position, parent.scope) != child_scope
        ):
            continue
        matches = {}
        for sentence in re.split(r"[.!?。！？;；\n]+", text):
            words = terms(sentence)
            for clause, wanted in zip(clause_specs(needs), scene_terms(needs)):
                if clause["entities"] and not any(
                    mentions(sentence, e) for e in clause["entities"]
                ):
                    continue
                score = len(words & wanted) / len(wanted) if wanted else 0
                if score >= 0.5:
                    matches[clause["id"]] = max(matches.get(clause["id"], 0), score)
        if matches:
            hints[uid] = [{"detail_position": position, "clauses": sorted(matches)}]
            priority[uid] = (len(matches), max(matches.values()), -position)
    return {uid: hints[uid] for uid in sorted(hints, key=priority.get, reverse=True)}


def _view_leader(batches, channel, view, allowed_ids=None):
    """Select a finite existing view head; never query or materialize more IDs."""
    eligible = set(allowed_ids) if allowed_ids is not None else None
    choices = {}
    for batch in batches:
        # Named native views only; custom sources keep existing fusion semantics.
        if batch.channel is not channel or batch.source != view:
            continue
        for rank, item in enumerate(batch.candidates, 1):
            if (
                eligible is not None
                and item.unit_id not in eligible
                or not math.isfinite(item.score)
            ):
                continue
            value = rank, -item.score, item.unit_id
            choices[item.unit_id] = min(choices.get(item.unit_id, value), value)
    return min(choices, key=choices.get) if choices else None


def detail_vector_leader(batches, allowed_ids=None):
    """Retain one native detailed-content hit without another query or body read."""
    return _view_leader(batches, RecallChannel.VECTOR, "vector_l2", allowed_ids)


def ranked_entry_ids(recalled, *, detail_keyword=False):
    """Rank existing IDs only; repeated layers do not add channel votes."""
    ranks, first = {}, {}
    for batch in recalled.batches:
        for rank, item in enumerate(batch.candidates, 1):
            first.setdefault(item.unit_id, len(first))
            key = item.unit_id, batch.channel
            ranks[key] = min(ranks.get(key, rank), rank)
    scores = {
        uid: sum(1 / (60 + rank) for (other, _), rank in ranks.items() if other == uid)
        for uid in first
    }
    ordered = sorted(
        first,
        key=lambda uid: (
            -scores[uid],
            ranks.get((uid, RecallChannel.VECTOR), float("inf")),
            first[uid],
        ),
    )
    # A broad keyword/coarse-summary agreement must not eliminate every
    # detailed-content vector hit before its body can even be checked.
    leader = detail_vector_leader(recalled.batches)
    if leader is not None:
        # Preserve the original detailed-vector and strongest fused entries.
        # An overview may complement them only within remaining entry slots;
        # repeated layers still do not receive extra channel votes.
        keyword = (
            _view_leader(recalled.batches, RecallChannel.KEYWORD, "keyword_l2")
            if detail_keyword
            else None
        )
        fused = next((uid for uid in ordered if uid not in {leader, keyword}), None)
        overview = _view_leader(recalled.batches, RecallChannel.VECTOR, "vector_l1")
        # An exact hit in the most detailed text can complement its vector neighbor
        # before overview matches use the remaining entry slots.
        heads = list(
            dict.fromkeys(uid for uid in (leader, keyword, fused, overview) if uid is not None)
        )
        ordered = [*heads, *(uid for uid in ordered if uid not in heads)]
    return ordered


def retain_single_slot_consensus(recalled, ordered, needs):
    """Prefer corroborated detail/overview evidence when there is no refill slot."""
    if not ordered or len(needs.facets) != 1 or needs.time_from or needs.time_to:
        return ordered
    batches = recalled.batches
    detailed = _view_leader(batches, RecallChannel.KEYWORD, "keyword_l2")
    if not detailed or not (
        detailed == _view_leader(batches, RecallChannel.VECTOR, "vector_l1")
        and detailed == _view_leader(batches, RecallChannel.KEYWORD, "keyword_l1")
    ):
        return ordered
    names = [name for name in needs.entities if is_named_entity(name)]
    facet = needs.facets[0]
    # The parser can keep a place or work in the literal scene without emitting
    # it as a separate entity. Require the whole literal phrase in the same sentence.
    scene_names = re.findall(r"\b[A-Z][\w-]+(?:\s+[A-Z][\w-]+)+\b", facet)
    names = list(dict.fromkeys([*names, *scene_names]))
    if not any(len(terms(name)) > 1 for name in names):
        return ordered
    wanted = terms(facet) - terms(" ".join(names))
    if not wanted:
        return ordered
    for batch in batches:
        if batch.channel is RecallChannel.KEYWORD and batch.source == "keyword_l1":
            for hit in batch.candidates[:1]:
                if hit.unit_id != detailed:
                    continue
                for sentence in re.split(r"[.!?。！？;；\n]+", hit.entry_keyword_text):
                    if all(mentions(sentence, name) for name in names) and terms(sentence) & wanted:
                        return [detailed, *(uid for uid in ordered if uid != detailed)]
    return ordered


def _coarse_keyword_head(batches, initial):
    """Consider a coarse lexical leader only if another native level supports it."""
    ranks_by_id = {}
    coarse_views = {"keyword_l0", "keyword_l1"}
    for batch in batches:
        if batch.channel is not RecallChannel.KEYWORD or batch.source not in coarse_views | {
            "keyword_l2"
        }:
            continue
        for rank, hit in enumerate(batch.candidates[:3], 1):
            if hit.unit_id in initial or not math.isfinite(hit.score):
                continue
            ranks = ranks_by_id.setdefault(hit.unit_id, {})
            ranks[batch.source] = min(ranks.get(batch.source, rank), rank)
    candidates = [
        (sum(ranks.values()), -len(ranks), uid)
        for uid, ranks in ranks_by_id.items()
        if len(ranks) >= 2 and any(ranks.get(view) == 1 for view in coarse_views)
    ]
    return min(candidates)[2] if candidates else None


def _complementary_entry(ordered, initial_limit, support):
    """Prefer at least two jointly supported topic words missing from earlier heads."""
    covered = set().union(
        *(part for uid in ordered[: initial_limit - 1] for part in support.get(uid, ()))
    )
    frequency = Counter(term for parts in support.values() for term in set().union(*parts))

    def gain(uid):
        return max(
            (
                sum(1 / frequency[term] for term in part - covered)
                for part in support.get(uid, ())
                if len(part - covered) >= 2
            ),
            default=0.0,
        )

    last = ordered[initial_limit - 1]
    best = max(ordered[initial_limit:], key=gain)
    value = gain(best)
    # Common words and ties do not justify perturbing a tested fused ordering.
    return best if value >= 0.75 and value - gain(last) >= 0.5 else None


def retain_attributed_entry(recalled, ordered, initial_limit, needs, *, diversify=False):
    """Replace one ungrounded entry with a native hit whose event matches the question."""
    if initial_limit < 2 or len(ordered) <= initial_limit:
        return ordered, None
    dated = bool(needs.time_from or needs.time_to)
    if (
        len(needs.entities) != 1
        or len(needs.facets) != 1
        or not is_named_entity(needs.entities[0])
        or (dated and not diversify)
    ):
        return ordered, None

    (wanted,) = facet_terms(needs)
    if not wanted:
        return ordered, None
    (entity,) = needs.entities
    encoded = {}
    conflicted = set()
    keyword_text = {}
    native_keyword_views = {"keyword_l0", "keyword_l1", "keyword_l2"}
    for batch in recalled.batches:
        if batch.channel not in {RecallChannel.VECTOR, RecallChannel.KEYWORD}:
            continue
        for hit in batch.candidates:
            if not math.isfinite(hit.score):
                continue
            if batch.channel is RecallChannel.KEYWORD and batch.source in native_keyword_views:
                if hit.entry_keyword_text:
                    keyword_text.setdefault(hit.unit_id, []).append(hit.entry_keyword_text)
            if hit.entry_semantics is None:
                continue
            existing = encoded.setdefault(hit.unit_id, hit.entry_semantics)
            if existing != hit.entry_semantics:
                conflicted.add(hit.unit_id)

    quality, support = {}, {}
    for uid in ordered:
        if uid not in encoded or uid in conflicted:
            continue
        try:
            features = decode_semantics(encoded[uid])
        except ValidationError:
            # Old or out-of-sync derived index attributes cannot decide entry membership.
            continue
        strength = 0.0
        for event in features.events:
            if not event.sources:
                continue
            signal = event_signals(features, event, needs)
            if (
                signal["window"] == "outside"
                or (needs.time_from or needs.time_to)
                and signal["window"] != "match"
            ):
                continue
            if diversify and entity_matches(features, (entity,), event.entity_keys):
                matched = terms(event.scene) & wanted
                if matched:
                    support.setdefault(uid, []).append(matched)
            for hit in signal["matches"]:
                if hit["entities"]:
                    strength = max(strength, hit["scene_score"])
        quality[uid] = strength

    for uid, texts in () if dated else keyword_text.items():
        if uid in conflicted:
            continue
        for text in texts:
            for sentence in re.split(r"[.!?。！？;；\n]+", text):
                if mentions(sentence, entity):
                    matched = terms(sentence) & wanted
                    strength = len(matched) / len(wanted)
                    if diversify and matched:
                        support.setdefault(uid, []).append(matched)
                    quality[uid] = max(quality.get(uid, 0.0), strength)

    threshold = 2 / 3
    tail = ordered[initial_limit - 1]
    if quality.get(tail, 0.0) >= threshold:
        return ordered, None
    alternatives = ordered[initial_limit:]
    selected = max(alternatives, key=lambda uid: quality.get(uid, 0.0))
    if quality.get(selected, 0.0) < threshold:
        selected = _complementary_entry(ordered, initial_limit, support) if diversify else None
        if selected is None:
            return ordered, None
    return [
        *ordered[: initial_limit - 1],
        selected,
        *(uid for uid in ordered[initial_limit - 1 :] if uid != selected),
    ], selected


def with_missing_clause_keyword_entry(
    domain, scope, query, recalled, ordered, initial_limit, needs
):
    """Probe the one missing clause in native text views before materialization."""
    if initial_limit < 2 or not 2 <= len(needs.facets) <= 3:
        return recalled, ordered, None
    if needs.time_from or needs.time_to:
        # A lexical match alone does not verify an explicit event window.
        return recalled, ordered, None

    specs = clause_specs(needs)
    desired = scene_terms(needs)
    names = [[name for name in spec["entities"] if is_named_entity(name)] for spec in specs]
    # Exclude shared vocabulary and names from the distinctive topic evidence.
    distinctive = [
        words
        - set().union(*(other for j, other in enumerate(desired) if j != i))
        - terms(" ".join(names[i]))
        for i, words in enumerate(desired)
    ]
    if any(not words or (len(words) < 2 and not names[i]) for i, words in enumerate(distinctive)):
        return recalled, ordered, None

    def supports(text, clause):
        wanted = distinctive[clause]
        for sentence in re.split(r"[.!?。！？;；\n]+", text):
            if all(mentions(sentence, name) for name in names[clause]) and len(
                terms(sentence) & wanted
            ) >= min(2, len(wanted)):
                return True
        return False

    initial = ordered[:initial_limit]
    support = {uid: set() for uid in initial}
    for batch in recalled.batches:
        if batch.channel is not RecallChannel.KEYWORD or batch.source not in {
            "keyword_l2",
            "keyword_l1",
        }:
            continue
        for hit in batch.candidates:
            if hit.unit_id in support and hit.entry_keyword_text:
                support[hit.unit_id].update(
                    i for i in range(len(specs)) if supports(hit.entry_keyword_text, i)
                )
    covered = set().union(*support.values())
    missing = set(range(len(specs))) - covered
    if len(missing) != 1:
        return recalled, ordered, None
    removals = [
        i
        for i in range(len(initial) - 1, 0, -1)
        if set().union(*(support[uid] for uid in initial if uid != initial[i])) == covered
    ]
    if not removals:
        return recalled, ordered, None
    (clause,) = missing
    spec = specs[clause]
    text = " ".join(
        [spec["scene"], *(name for name in names[clause] if not mentions(spec["scene"], name))]
    )
    result = domain.recall(
        scope, replace(query, rewritten=text), channels=[RecallChannel.KEYWORD], recall_limit=2
    )
    if result.errors:
        return recalled, ordered, None

    selected = next(
        (
            hit
            for batch in result.batches
            if batch.source == "keyword_l2"
            for hit in batch.candidates
            if hit.unit_id not in initial
            and math.isfinite(hit.score)
            and hit.score > 0
            and supports(hit.entry_keyword_text, clause)
        ),
        None,
    )
    if selected is None:
        return recalled, ordered, None
    offset = removals[0]
    uid = selected.unit_id
    order = [*ordered[:offset], uid, *(item for item in ordered[offset:] if item != uid)]
    if uid in ordered:
        # The original candidate already carries the same index evidence. Avoid
        # giving a second keyword vote merely for continuing a separate clause.
        return recalled, order, uid
    lexical = [
        replace(b, candidates=[hit for hit in b.candidates if hit.unit_id == uid])
        for b in result.batches
        if any(hit.unit_id == uid for hit in b.candidates)
    ]
    return replace(recalled, batches=[*recalled.batches, *lexical]), order, uid


def _temporal_anchor_candidate(recalled, ordered, initial, reserved, query):
    """Keep a deferred event branch whose native detailed text binds the query anchors."""
    needs = needs_for(query)
    if (
        needs.mode != "temporal"
        or needs.time_from
        or needs.time_to
        or not re.search(r"\b(?:before|after|between|since)\b", query.raw, re.I)
        or len(needs.facets) != 1
        or len(initial) < 2
    ):
        return None
    names = [name for name in clause_specs(needs)[0]["entities"] if is_named_entity(name)]
    if not 2 <= len(names) <= 4 or not any(len(terms(name)) >= 2 for name in names):
        return None
    qualifying = []
    for batch in recalled.batches:
        if batch.channel is not RecallChannel.KEYWORD or batch.source != "keyword_l2":
            continue
        for rank, hit in enumerate(batch.candidates[:4], 1):
            if not math.isfinite(hit.score) or hit.score <= 0 or not hit.entry_keyword_text:
                continue
            sentences = re.split(r"[.!?。！？;；\n]+", hit.entry_keyword_text)
            if any(all(mentions(sentence, name) for name in names) for sentence in sentences):
                qualifying.append((rank, hit.unit_id))
    anchored = {uid for _, uid in qualifying}
    # Leave a two-operand query alone if two initial branches already support its
    # anchors or if the existing deferred reservation already supplies the gap.
    if len(anchored.intersection(initial)) >= 2 or reserved in anchored:
        return None
    eligible = [(rank, uid) for rank, uid in qualifying if uid not in initial and uid in ordered]
    return min(eligible)[1] if eligible else None


@dataclass(frozen=True)
class EntryProbePool:
    """Request-local ID metadata, bound to the original recall scope."""

    scope: Scope
    initial_ids: tuple[str, ...]
    alternative_ids: tuple[str, ...]
    corroborated_alternative: str | None = None
    deferred_detail_keyword: str | None = None
    coarse_keyword_alternative: str | None = None
    temporal_keyword_alternative: str | None = None
    attributed_entry: str | None = None
    temporal_anchor_alternative: str | None = None

    @classmethod
    def from_recalled(
        cls,
        scope,
        recalled,
        initial_limit,
        summary_limit,
        *,
        detail_keyword=False,
        corroborate=False,
        prefer_deferred_keyword=False,
        prefer_coarse_keyword=False,
        prefer_temporal_keyword=False,
        ordered_ids=None,
        attributed_entry=None,
        anchor_query=None,
    ):
        order = (
            ranked_entry_ids(recalled, detail_keyword=detail_keyword)
            if ordered_ids is None
            else ordered_ids
        )
        ordered = order[: max(0, summary_limit)]
        initial = tuple(ordered[:initial_limit])
        alternatives = tuple(ordered[initial_limit:])
        corroborated_alternative = None
        deferred_detail_keyword = None
        if corroborate and 2 <= summary_limit - len(initial) <= 4:
            views = {}
            native = {
                RecallChannel.KEYWORD: {"keyword_l1", "keyword_l2"},
                RecallChannel.VECTOR: {"vector_l1", "vector_l2"},
            }
            for batch in recalled.batches:
                if batch.source not in native.get(batch.channel, ()):
                    continue
                for hit in batch.candidates[:2]:
                    if math.isfinite(hit.score):
                        views.setdefault(hit.unit_id, set()).add(batch.source)
            corroborated_alternative = next(
                (
                    uid
                    for uid in alternatives
                    if len(views.get(uid, ())) >= 2 and views[uid] & {"keyword_l2", "vector_l2"}
                ),
                None,
            )
        if prefer_deferred_keyword and initial_limit >= 3 and alternatives:
            heads = [
                replace(batch, candidates=batch.candidates[:initial_limit])
                for batch in recalled.batches
                if batch.source == "keyword_l2"
            ]
            outside = {hit.unit_id for batch in heads for hit in batch.candidates} - set(ordered)
            deferred_detail_keyword = _view_leader(
                heads, RecallChannel.KEYWORD, "keyword_l2", outside
            )
        coarse_keyword_alternative = (
            _coarse_keyword_head(recalled.batches, initial)
            if prefer_coarse_keyword and alternatives and 2 <= summary_limit - initial_limit <= 4
            else None
        )
        temporal_keyword_alternative = None
        if prefer_temporal_keyword and alternatives and 2 <= summary_limit - initial_limit <= 4:
            head = _view_leader(recalled.batches, RecallChannel.KEYWORD, "keyword_l1")
            if head not in initial:
                temporal_keyword_alternative = head
        anchor_alternative = (
            _temporal_anchor_candidate(
                recalled, order, initial, corroborated_alternative, anchor_query
            )
            if anchor_query is not None and alternatives and 2 <= summary_limit - len(initial) <= 8
            else None
        )
        return cls(
            scope,
            initial,
            alternatives,
            corroborated_alternative,
            deferred_detail_keyword,
            coarse_keyword_alternative,
            temporal_keyword_alternative,
            attributed_entry,
            anchor_alternative,
        )


def limit_recalled(recalled, limit, *, detail_keyword=False, ordered_ids=None):
    """Bound IDs before materialization without changing channel evidence."""
    ordered = (
        ranked_entry_ids(recalled, detail_keyword=detail_keyword)
        if ordered_ids is None
        else ordered_ids
    )
    keep = set(ordered[: max(0, limit)])
    return RecallResult(
        batches=[
            replace(batch, candidates=[c for c in batch.candidates if c.unit_id in keep])
            for batch in recalled.batches
        ],
        errors=list(recalled.errors),
    )


def candidate_limits(domain, policy, scope, query):
    """Compute pool caps from index metadata before any candidate body read."""
    from jiuwen_memory.common.errors import ValidationError
    from jiuwen_memory.common.type_def import HierarchyRole, and_merge
    from jiuwen_memory.common.type_def.hierarchy_query import HierarchyQuery
    from jiuwen_memory.retrieval.retriever_impl.predicate_builder import (
        build_hierarchy_filters,
        build_system_filters,
    )

    limits = {
        "initial_limit": policy.initial_candidate_limit,
        "summary_limit": policy.summary_candidate_limit,
        "source_limit": policy.source_candidate_limit,
        "source_population": None,
        "summary_population": None,
    }
    for kind, ratio, role in (
        ("summary", policy.summary_candidate_ratio, query.hierarchy_role),
        (
            "source",
            policy.source_candidate_ratio if policy.include_original else 0,
            HierarchyRole.SNAPSHOT,
        ),
    ):
        if not ratio:
            continue
        counted = replace(query, hierarchy_role=role)
        counted.scalar_filters = and_merge(
            query.recheck_filters,
            [
                *build_hierarchy_filters(HierarchyQuery.from_query(counted)),
                *build_system_filters(
                    query.as_of, query.time_from, query.time_to, query.include_archived
                ),
            ],
        )
        population = domain.indexed_population(scope, counted)
        if type(population) is not int or population < 0:
            raise ValidationError("indexed population must be a nonnegative integer")
        limits[f"{kind}_population"] = population
        # A nonempty bank smaller than 1/ratio may return one indivisible node.
        limit = max(1, int(population * ratio)) if population else 0
        limits[f"{kind}_limit"] = min(limits[f"{kind}_limit"], limit)
    entry_room = limits["summary_limit"]
    if policy.summary_candidate_ratio and policy.use_relations and entry_room > 1:
        # Redistribute a fixed read budget between entry quality and graph
        # navigation; this fraction never enlarges the total candidate pool.
        entry_room = max(1, int(entry_room * policy.initial_summary_fraction))
    limits["initial_limit"] = min(limits["initial_limit"], entry_room)
    return limits
