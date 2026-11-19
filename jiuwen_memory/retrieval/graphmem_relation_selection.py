"""Ground edge choices in attributed query scenes, entities and event intervals."""

from __future__ import annotations

from jiuwen_memory.common.graphmem_semantics import (
    canonical,
    interval_gap,
    iso_time,
    read_semantics,
)
from jiuwen_memory.retrieval.graphmem_query import terms
from jiuwen_memory.retrieval.graphmem_relation_plan import (
    entity_matches,
    event_signals,
    node_signals,
)


def edge_hint(left, edge, needs, program):
    """Conservative metadata preselection; the target is NOT yet read/validated."""
    attrs = set(edge.properties.get("attributes", ())) & set(program.relations)
    features = read_semantics(left)
    requested = set(entity_matches(features, needs.entities))
    shared = set(edge.properties.get("shared_entities", ()))
    matched = [e.name for e in features.entities if e.match_keys & shared
               and (not needs.entities or e.name in requested)] if features else []
    if attrs == {"entity"} and needs.entities and not matched:
        return None
    # An entity-only edge with no represented participant is not usable by this
    # attributed selector. Semantic edges can still recover incomplete metadata.
    if attrs == {"entity"} and not matched:
        return None
    return {"shared_query_entities": matched, "attributes": sorted(attrs)}


def _time_reason(a, b, edge, needs, program):
    if not a or not b:
        return None
    matched = edge.properties.get("matched_events", [])
    pairs = [{entry.get("left"), entry.get("right")} for entry in matched
             if isinstance(entry, dict)]
    choices = []
    anchor_words = terms(program.time_anchor) - set().union(
        *(terms(e) for e in needs.entities))
    for first in a.events:
        for second in b.events:
            if (not first.time.known or not second.time.known
                    or {first.key, second.key} not in pairs):
                continue
            fa, fb = event_signals(a, first, needs), event_signals(b, second, needs)
            if not (fa["matches"] or fb["matches"]) or fb["window"] == "outside":
                continue
            # Do not cross into an explicitly different participant's event.
            if needs.entities and second.entity_keys and not entity_matches(
                    b, needs.entities, second.entity_keys):
                continue
            condition = "adjacent"
            anchor_id = ""
            if program.time_anchor:
                anchored = []
                for anchor, target in ((first, second), (second, first)):
                    overlap = len(anchor_words & terms(anchor.scene)) / max(1, len(anchor_words))
                    if overlap < 0.5:
                        continue
                    holds = (iso_time(target.time.end) <= iso_time(anchor.time.start)
                             if program.time_relation == "before" else
                             iso_time(target.time.start) >= iso_time(anchor.time.end))
                    if holds:
                        anchored.append((overlap, anchor.key))
                if not anchored:
                    continue
                _, anchor_id = max(anchored)
                condition = program.time_relation
            gap = interval_gap(first.time, second.time)
            clauses = sorted({h["clause"] for f in (fa, fb) for h in f["matches"]})
            score = sum(h["scene_score"] for f in (fa, fb) for h in f["matches"])
            choices.append((-len(clauses), -score, gap, first.key, second.key, {
                "gap_seconds": gap, "from_event": first.key, "to_event": second.key,
                "from_interval": [first.time.start, first.time.end],
                "to_interval": [second.time.start, second.time.end],
                "condition": condition, "anchor_event": anchor_id, "clauses": clauses,
                "window": fb["window"],
            }))
    return min(choices, key=lambda c: c[:5])[-1] if choices else None


def _shared_entities(a, b, edge, needs):
    if not a or not b:
        return []
    shared = set(edge.properties.get("shared_entities", ()))
    target_keys = set().union(*(e.match_keys for e in b.entities))
    requested = {canonical(e) for e in needs.entities}
    return [e.name for e in a.entities if e.match_keys & shared & target_keys and (
        not requested or requested & {canonical(n) for n in (e.name, *e.aliases)})]


def relation_reason(left, right, edge, needs, program, query_similarity=0, *,
                    allow_semantic_probe=False, allow_entity_probe=False):
    attrs = set(edge.properties.get("attributes", ())) & set(program.relations)
    a, b = read_semantics(left), read_semantics(right)
    support = node_signals(right, needs)
    reason = {}
    names = _shared_entities(a, b, edge, needs) if "entity" in attrs else []
    if "entity" in attrs and a and b and support["clauses"]:
        if names:
            reason["entities"] = names[:4]
            reason["scene_clauses"] = support["clauses"]
    if "semantic" in attrs and support["clauses"]:
        reason["scene_clauses"] = support["clauses"]
    if "temporal" in attrs:
        temporal = _time_reason(a, b, edge, needs, program)
        if temporal:
            reason["event_time"] = temporal
    entity_probe = allow_entity_probe and bool(names)
    if (not reason and allow_semantic_probe and query_similarity > 0
            and ("semantic" in attrs or entity_probe)):
        # At most one ranked semantic exploration per request, explicitly unknown
        # clause support. Positive cosine alone never becomes an entity/time proof.
        has_explicit_other = (needs.entities and b and b.entities
                              and not support["entities"])
        outside = support["event_bindings"] and all(
            e["window"] == "outside" for e in support["event_bindings"])
        if not has_explicit_other and not outside:
            reason["semantic_probe"] = True
            if "semantic" not in attrs:
                # The stored edge proves shared context, not the query's scene.
                # Keep this under the same one-probe allowance and never turn
                # its embedding rank into a clause/entity/time coverage claim.
                reason["probe_relation"] = "entity"
                reason["shared_relation_entities"] = names[:4]
    if reason:
        reason["query_scene_similarity"] = query_similarity
        reason["clause_transition"] = {
            "from": node_signals(left, needs)["clauses"], "to": support["clauses"]}
    return reason
