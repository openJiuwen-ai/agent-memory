"""Render already disclosed GraphMem evidence as linked conversation passages."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime


def _payloads(bundle):
    """Match each native E header to its published content digest before regrouping."""
    refs = bundle["sources"]
    if not refs:
        return []
    _, marker, text = bundle["rendered_context"].partition("\n\n[Evidence]\n")
    if not marker:
        raise ValueError("Expected a native relational evidence section")
    payloads = []
    offset = 0
    for i, source in enumerate(refs, 1):
        header_end = text.index("\n", offset)
        header = json.loads(text[offset:header_end])
        if header.get("ref") != f"E{i}" or header.get("kind") not in {"summary", "source"}:
            raise ValueError("Evidence header differs from the published reference order")
        start = header_end + 1
        next_prefix = '\n\n{"ref":"E' + str(i + 1) + '","kind":'
        end = len(text) if i == len(refs) else text.find(next_prefix, start)
        while end >= 0:
            content = text[start:end]
            if hashlib.sha256(content.encode()).hexdigest() == source["content_sha256"]:
                break
            end = -1 if i == len(refs) else text.find(next_prefix, end + 1)
        if end < 0:
            raise ValueError("Evidence text differs from its published content digest")
        payloads.append((header, source["scope"], content))
        offset = end + 2
    return payloads


def _relation_words(reason, facets):
    labels = []
    if reason.get("entities"):
        labels.append("both summaries mention " + ", ".join(reason["entities"]))
    scenes = [facets[c] for c in reason.get("scene_clauses", []) if c in facets]
    if scenes:
        labels.append("similar scene concerning " + ", ".join(scenes))
    time = reason.get("event_time")
    if time:
        gap = time.get("gap_seconds")
        if isinstance(gap, (int, float)) and gap >= 0:
            labels.append(
                "indexed event intervals overlap"
                if gap == 0
                else f"indexed event intervals are {gap / 86400:.2g} days apart"
            )
    if not labels and reason.get("semantic_probe"):
        labels.append("related topic suggested by the retrieval index")
    return " and ".join(labels) or "related conversation summaries"


def _message_order(header):
    stamp = header.get("message_time")
    if not stamp:
        return (1, "")
    moment = datetime.fromisoformat(stamp)
    if moment.tzinfo is None:
        return (0, moment.isoformat())
    return (0, moment.astimezone(UTC).replace(tzinfo=None).isoformat())


def _shared_message_prefix(items, session):
    """Lift an identical source attribution prefix, leaving each speaker inline."""
    if not session:
        return ""
    prefixes = []
    for header, text in items:
        if header["kind"] != "source":
            continue
        speaker = header.get("speaker", "")
        first = text.split("\n", 1)[0]
        prefix, separator, rest = first.partition("] ")
        if (
            not speaker
            or not separator
            or not prefix.startswith(f"[Session {session} | ")
            or not rest.startswith(f"{speaker}: ")
        ):
            return ""
        prefixes.append(prefix + separator)
    return prefixes[0] if len(prefixes) > 1 and len(set(prefixes)) == 1 else ""


_RELATIVE_SOURCE_DAY = re.compile(
    r"\b(?:yesterday|last week|(?:[1-9][0-9]?|one|two|three|four|five|six|seven|eight|nine|ten|a) "
    r"(?:days?|weeks?) ago)\b",
    re.I,
)


def _relative_source_notes(payloads, graph):
    """Annotate literal date phrases in the most relevant original turns."""
    from jiuwen_memory.common.graphmem_semantics import resolved_relative_day, source_clock
    from jiuwen_memory.retrieval.graphmem_query import terms

    plan = graph.get("query_plan", {})
    if not plan.get("facets"):
        return {}

    def stems(words):
        return {re.sub(r"(?:ing|ed)$", "", word) if len(word) > 5 else word for word in words}

    wanted = stems(terms(" ".join(plan["facets"])))
    anchors = {node["ref"]: node.get("reason", {}) for node in graph.get("nodes", [])}
    choices = []
    for header, _, text in payloads:
        if header["kind"] != "source" or not header.get("message_time"):
            continue
        speaker = header.get("speaker", "")
        if speaker.casefold() == "assistant":
            continue
        stamp = header["message_time"]
        statement = text.split("[Media shared by", 1)[0]
        if statement.startswith("[Session ") and "] " in statement:
            statement = statement.split("] ", 1)[1]
        if speaker and statement.startswith(speaker + ": "):
            statement = statement.removeprefix(speaker + ": ")
        phrases = list(_RELATIVE_SOURCE_DAY.finditer(statement))
        if len(phrases) != 1 or re.search(r'["“”]', statement):
            continue
        reason = anchors.get(header["ref"], {})
        overlap = (wanted - stems(terms(speaker))) & stems(terms(statement))
        if not overlap and not reason.get("parent_event_references"):
            continue
        if not overlap and not reason.get("clauses"):
            # A parent hint alone does not place the relative phrase on the requested event.
            continue
        match = phrases[0]
        phrase = match.group(0).lower()
        source_day = source_clock(stamp)
        date = source_day.date().isoformat()
        if phrase == "last week":
            note = f"“{phrase}” is relative to {date}; exact day unspecified."
        else:
            when = resolved_relative_day(phrase, stamp)
            if when is None:
                continue
            local_date = when.astimezone(source_day.tzinfo).date().isoformat()
            approx = "about " if "week" in phrase else ""
            note = f"“{phrase}” from {date} points to {approx}{local_date}."
        confidence = (
            bool(reason.get("parent_event_references") or reason.get("clauses")),
            len(overlap),
        )
        choices.append((confidence, header["ref"], note))
    choices.sort(key=lambda item: item[0], reverse=True)
    return {ref: note for _, ref, note in choices[:4]}


def _nearby_original(anchor, neighbor):
    if neighbor["kind"] != "source" or not neighbor.get("message_time"):
        return False
    t1 = datetime.fromisoformat(anchor["message_time"])
    t2 = datetime.fromisoformat(neighbor["message_time"])
    return (
        abs(
            (
                t1.replace(tzinfo=t1.tzinfo or UTC) - t2.replace(tzinfo=t2.tzinfo or UTC)
            ).total_seconds()
        )
        <= 120
    )


def _event_blocks(items, source_topics):
    """Keep a matched turn and up to one original on either side in one passage."""
    blocks = []
    for i, (head, _) in enumerate(items):
        clauses = source_topics.get(head["ref"])
        if head["kind"] != "source" or not clauses or not head.get("message_time"):
            continue
        first, stop = i, i + 1
        if i and _nearby_original(head, items[i - 1][0]):
            first -= 1
        if stop < len(items) and _nearby_original(head, items[stop][0]):
            stop += 1
        if blocks and first < blocks[-1][1]:
            previous_first, previous_stop, previous_clauses = blocks.pop()
            blocks.append((previous_first, max(previous_stop, stop), previous_clauses | clauses))
        else:
            blocks.append((first, stop, set(clauses)))
    result = {}
    for first, stop, clauses in blocks:
        result[first] = clauses
        if stop < len(items):
            result.setdefault(stop, set())
    return result


def render_evidence_passages(
    bundle,
    *,
    collapse_shared_prefix=None,
    chronological=False,
    relative_source_clock=False,
    group_event_turns=False,
) -> str:
    """Keep each source, with chronological passages or a shared clock when enabled."""
    if is_dataclass(bundle):
        bundle = asdict(bundle)
    navigation = bundle.get("navigation") or {}
    graph = navigation.get("evidence_graph")
    if not graph:
        return bundle["rendered_context"]
    if collapse_shared_prefix is None:
        collapse_shared_prefix = navigation.get("query_mode") == "fact"
    payloads = _payloads(bundle)
    if not payloads:
        return ""
    clock_notes = _relative_source_notes(payloads, graph) if relative_source_clock else {}
    parent_refs = defaultdict(list)
    for edge in graph["edges"]:
        if edge["kind"] == "contains" and edge.get("reason", {}).get("verified_child_link"):
            parent_refs[edge["target"]].append(edge["source"])
    groups_for_ref = defaultdict(set)
    group_for_source = {}
    for h, scope, _ in payloads:
        if h["kind"] != "source":
            continue
        group = tuple(scope.get(k, "") for k in ("org", "space", "user", "agent", "session"))
        if not group[-1]:
            group += (h["ref"],)
        group_for_source[h["ref"]] = group
        pending = [h["ref"]]
        while pending:
            ref = pending.pop()
            if group not in groups_for_ref[ref]:
                groups_for_ref[ref].add(group)
                pending.extend(parent_refs[ref])
    groups = defaultdict(list)
    for h, scope, text in payloads:
        ref = h["ref"]
        if ref in group_for_source:
            group = group_for_source[ref]
        elif len(groups_for_ref[ref]) == 1:
            group = next(iter(groups_for_ref[ref]))
        else:
            group = ("summary", ref)
        groups[group].append((h, text))
        groups_for_ref[ref].add(group)

    order = {g: i for i, g in enumerate(groups)}
    first_time = {
        g: min((h["message_time"] for h, _ in items if h.get("message_time")), default="~")
        for g, items in groups.items()
    }
    facets = {f"C{i}": f for i, f in enumerate(graph["query_plan"]["facets"], 1)}
    links = defaultdict(set)
    relations = []
    for edge in graph["text_relation_edges"]:
        if edge.get("reason", {}).get("navigation_only_backstop"):
            continue
        left, right = (groups_for_ref[edge[end]] for end in ("source", "target"))
        words = _relation_words(edge.get("reason", {}), facets)
        relations.append((left, right, words, edge["source"], edge["target"]))
        # Only uniquely attributable endpoints establish adjacency between passages.
        if len(left) == len(right) == 1:
            a, b = next(iter(left)), next(iter(right))
            if a != b:
                links[a].add(b)
                links[b].add(a)

    group_clock = {g: min(_message_order(h) for h, _ in items) for g, items in groups.items()}
    priorities = sorted(groups, key=lambda g: (group_clock[g], order[g]))
    if chronological:
        passages = priorities
    else:
        passages, visited = [], set()
        for root in priorities:
            if root in visited:
                continue
            pending = deque([root])
            visited.add(root)
            while pending:
                group = pending.popleft()
                passages.append(group)
                for nxt in sorted(links[group], key=lambda g: (group_clock[g], order[g])):
                    if nxt not in visited:
                        pending.append(nxt)
                        visited.add(nxt)
    numbers = {g: i for i, g in enumerate(passages, 1)}
    notes = defaultdict(list)
    for left, right, words, a, b in relations:
        reached = left | right
        target = max(reached, key=numbers.get) if reached else passages[0]

        def describe(endpoint, ref):
            ids = sorted(numbers[g] for g in endpoint)
            if not ids:
                return f"index node {ref}"
            return ("passage " if len(ids) == 1 else "passages ") + ", ".join(map(str, ids))

        if left == right == {target}:
            note = f"Two summaries in this passage are linked: {words}."
        elif len(left) == len(right) == 1:
            other = next(iter(reached - {target}))
            note = f"Index link to passage {numbers[other]}: {words}."
        else:
            note = f"Index link between {describe(left, a)} and {describe(right, b)}: {words}."
        notes[target].append(note)
    topics = defaultdict(set)
    for key in ("clause_bindings", "navigation_clause_bindings"):
        for clause, refs in graph.get(key, {}).items():
            if clause in facets:
                for ref in refs:
                    for group in groups_for_ref[ref]:
                        topics[group].add(clause)
    source_topics = defaultdict(set)
    if group_event_turns:
        for clause, refs in graph.get("clause_bindings", {}).items():
            if clause in facets:
                for ref in refs:
                    source_topics[ref].add(clause)
    sections = []
    for group in passages:
        number = numbers[group]
        items = sorted(
            groups[group],
            key=lambda pair: (
                (pair[0]["kind"] == "summary") if chronological else (pair[0]["kind"] != "summary"),
                _message_order(pair[0]),
                int(pair[0]["ref"][1:]),
            ),
        )
        date = first_time[group][:10]
        suffix = f" | conversation on {date}" if date != "~" else " | summary"
        title = f"Passage {number}{suffix}"
        session = group[4] if len(group) >= 5 else ""
        shared = _shared_message_prefix(items, session) if collapse_shared_prefix else ""
        if shared:
            title += " | message time and session for all source turns: " + shared.rstrip()
        lines = [title]
        if topics[group] and not group_event_turns:
            topic = " / ".join(facets[k] for k in facets if k in topics[group])
            lines.append(f"Retrieval topic: {topic}.")
        lines.extend(dict.fromkeys(notes[group]))
        event_blocks = _event_blocks(items, source_topics) if group_event_turns else {}
        for index, (header, text) in enumerate(items):
            if index in event_blocks:
                clauses = event_blocks[index]
                if clauses:
                    topic = " / ".join(facets[key] for key in facets if key in clauses)
                    lines.append(f"Related turns to check for {topic}:")
                else:
                    lines.append("")
            ref = header["ref"]
            if header["kind"] == "summary":
                span = " to ".join(
                    str(header[k])[:10] for k in ("coverage_start", "coverage_end") if header.get(k)
                )
                label = "Index summary" + (f" covering {span}" if span else "")
                lines.append(f"[{ref}] {label}: {text}")
            else:
                # The benchmark stores its own session/date/speaker in the source. Other
                # callers may supply that information in the native evidence header.
                speaker = header.get("speaker", "")
                has_attribution = (
                    speaker
                    and text.startswith(f"[Session {session} | ")
                    and f"] {speaker}: " in text.split("\n", 1)[0]
                )
                attribution = (
                    ""
                    if has_attribution
                    else " ".join(x for x in (header.get("message_time", ""), speaker) if x)
                )
                lines.append(
                    f"[{ref}] "
                    + (attribution + ": " if attribution else "")
                    + text.removeprefix(shared)
                )
                if ref in clock_notes:
                    lines.append("  Source clock: " + clock_notes[ref])
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
