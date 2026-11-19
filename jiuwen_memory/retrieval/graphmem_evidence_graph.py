"""Render selected evidence in graph order, counting graph text in the same budget."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from copy import deepcopy
from dataclasses import asdict, replace

from jiuwen_memory.common.graphmem_semantics import iso_time
from jiuwen_memory.common.type_def import HierarchyRole
from jiuwen_memory.retrieval.evidence_packing import EvidenceBundle, UnitEvidenceRef, _render
from jiuwen_memory.retrieval.expander import node_key


def graph_order(selected, plan):
    # Traverse verified paths even when an intermediate summary cannot fit in
    # the final payload. Return only admitted evidence, never hidden content.
    by_key = {node_key(c.unit): c for c in plan.candidates}
    admitted = {node_key(c.unit) for c in selected}
    adjacency = {key: [] for key in by_key}
    for edge in plan.links:
        a, b = edge["source"], edge["target"]
        if a in by_key and b in by_key:
            adjacency[a].append((b, edge["kind"]))
            if edge["kind"] == "related":
                adjacency[b].append((a, edge["kind"]))
    rank = {node_key(c.unit): i for i, c in enumerate(plan.candidates)}

    def order(key):
        times = plan.reasons.get(key, {}).get("event_intervals", [])
        stamps = [iso_time(w["start"]).timestamp() for w in times]
        numeric = min(stamps, default=0)
        if plan.program["order"].endswith("descending"):
            numeric = -max(stamps, default=0)
        return (not bool(stamps), numeric, rank.get(key, 0))

    ordered, seen = [], set()

    def visit(key):
        if key in seen:
            return
        seen.add(key)
        if key in admitted:
            ordered.append(by_key[key])
        # Finish a verified branch before crossing into another conversation.
        # Candidate priority controls admission, not the displayed conversation
        # order. Native child order also preserves original multi-part messages.
        parent = by_key[key].unit
        siblings = {}
        for index, uid in enumerate(parent.hierarchy.child_ids):
            child_scope = parent.hierarchy.child_scope_at(index, parent.scope)
            for target, kind in adjacency[key]:
                child = by_key[target].unit
                if kind == "contains" and child.id == uid and child.scope == child_scope:
                    siblings[target] = index

        def next_step(step):
            target, kind = step
            temporal = order(target)[:2] if plan.program["order"].startswith("event_time") else ()
            return (
                kind != "contains",
                *temporal,
                siblings.get(target, rank.get(target, 0)),
                rank.get(target, 0),
            )

        children = sorted(adjacency[key], key=next_step)
        for child, _ in children:
            visit(child)

    roots = list(by_key)
    if plan.program["order"].startswith("event_time"):
        roots.sort(key=order)
    for key in roots:
        visit(key)
    return ordered


def navigation_paths(plan, admitted):
    """Shortest verified paths, including omitted relation-only connectors."""
    known = {node_key(c.unit) for c in plan.candidates}
    adjacent = {key: [] for key in known}
    for edge in plan.links:
        a, b = edge["source"], edge["target"]
        if a in known and b in known:
            adjacent[a].append((b, edge, "forward"))
            adjacent[b].append((a, edge, "reverse"))
    roots = [
        node_key(c.unit)
        for c in plan.candidates
        if plan.reasons.get(node_key(c.unit), {}).get("selected_by")
        in {"initial_vector_keyword_match", "deferred_vector_keyword_match"}
    ]
    if not roots and plan.candidates:
        roots = [node_key(plan.candidates[0].unit)]
    parents = dict.fromkeys(roots)
    queue = deque(roots)
    while queue:
        current = queue.popleft()
        for neighbor, edge, direction in adjacent[current]:
            if neighbor not in parents:
                parents[neighbor] = current, edge, direction
                queue.append(neighbor)
    paths, required = {}, set(admitted)
    for target in admitted:
        steps, current = [], target
        while parents.get(current) is not None:
            previous, edge, direction = parents[current]
            steps.append((previous, current, edge, direction))
            required.add(previous)
            current = previous
        paths[target] = list(reversed(steps))
    return required, paths


def render_graph(selected, plan):
    if not selected:
        return "", {"nodes": [], "edges": [], "query_plan": plan.program}
    refs = {node_key(c.unit): f"E{i}" for i, c in enumerate(selected, 1)}
    nodes = [
        {
            "ref": refs[node_key(c.unit)],
            "kind": "source" if c.unit.hierarchy.role is HierarchyRole.SNAPSHOT else "summary",
            "reason": plan.reasons.get(node_key(c.unit), {}),
        }
        for c in selected
    ]
    # Retain compact connectors, not undisclosed summary text. N references are
    # navigation-only, have separate provenance and cannot serve as evidence.
    required, paths = navigation_paths(plan, refs)
    for _ in range(len(plan.candidates)):
        parents = {
            e["source"] for e in plan.links if e["kind"] == "contains" and e["target"] in required
        }
        if parents <= required:
            break
        required.update(parents)
    navigation_sources = []
    for item in plan.candidates:
        key = node_key(item.unit)
        if key not in required or key in refs:
            continue
        ref = f"N{len(navigation_sources) + 1}"
        refs[key] = ref
        nodes.append(
            {
                "ref": ref,
                "kind": "navigation_only",
                "reason": {
                    k: v
                    for k, v in plan.reasons.get(key, {}).items()
                    if k in {"selected_by", "clauses"}
                },
            }
        )
        navigation_sources.append(
            {
                "ref": ref,
                "source": asdict(
                    UnitEvidenceRef(
                        deepcopy(item.unit.scope),
                        item.unit.id,
                        hashlib.sha256(item.unit.content.encode()).hexdigest(),
                    )
                ),
            }
        )
    edges, seen = [], set()
    for edge in plan.links:
        a, b = edge["source"], edge["target"]
        identity = (a, b, edge["kind"])
        if a in refs and b in refs and identity not in seen:
            seen.add(identity)
            edges.append(
                {
                    **{k: v for k, v in edge.items() if k not in {"source", "target"}},
                    "source": refs[a],
                    "target": refs[b],
                }
            )
    compact_plan = {
        "combine": plan.program["combine"],
        "order": plan.program["order"],
        "needs": {f"C{i + 1}": scene for i, scene in enumerate(plan.program["facets"])},
        "entities": plan.program["entities"],
        "time": [plan.program["time_from"], plan.program["time_to"]],
    }
    if plan.program.get("time_anchor"):
        compact_plan["event_order"] = {
            "target": plan.program["time_relation"],
            "anchor": plan.program["time_anchor"],
        }
    lines = [
        "[Query plan] " + json.dumps(compact_plan, ensure_ascii=False, separators=(",", ":")),
        "[Evidence graph]",
    ]
    for node in nodes:
        if node["kind"] == "source":
            continue  # Full source headers already contain the E identifier.
        reason = node["reason"]
        labels = {
            "initial_vector_keyword_match": "entry",
            "query_relevant_relation": "relation",
            "deferred_vector_keyword_match": "index fallback",
            "verified_parent_for_relation_navigation": "parent",
            "query_selected_child_summary": "child",
        }
        why = labels.get(reason.get("selected_by"), "selected")
        lines.append(
            f"{node['ref']} {node['kind']} ({why}; " + ",".join(reason.get("clauses", [])) + ")"
        )
    contains = {}
    components = {ref: ref for ref in refs.values()}

    def component(ref):
        while components[ref] != ref:
            ref = components[ref]
        return ref

    def connect(a, b):
        a, b = component(a), component(b)
        if a == b:
            return False
        components[b] = a
        return True

    for edge in edges:
        if edge["kind"] == "contains":
            contains.setdefault(edge["source"], []).append(edge["target"])
            connect(edge["source"], edge["target"])
    # Show a connecting forest instead of repeated cycles. Detailed structured
    # output still contains every selected relation and its selection reasons.
    text_relations = []
    related = sorted(
        (e for e in edges if e["kind"] == "related"),
        key=lambda e: (
            -bool(e["reason"].get("join_clauses")),
            -bool(e["reason"].get("entities")),
            -e["reason"].get("query_scene_similarity", 0),
        ),
    )
    for edge in related:
        if not connect(edge["source"], edge["target"]):
            continue
        text_relations.append(edge)
        reason = edge["reason"]
        concise = []
        if reason.get("entities"):
            concise.append("entity=" + ",".join(reason["entities"]))
        if reason.get("scene_clauses"):
            concise.append("scene=" + ",".join(reason["scene_clauses"]))
        elif reason.get("semantic_probe"):
            concise.append(
                "query_probe(entity)"
                if reason.get("probe_relation") == "entity"
                else "semantic_probe"
            )
        if "event_time" in reason:
            window = reason["event_time"]
            if window.get("anchor_event"):
                anchor = (
                    edge["source"]
                    if window["anchor_event"] == window["from_event"]
                    else edge["target"]
                )
                concise.append(f"time={window['condition']}(anchor={anchor})")
            concise.append(f"event_gap={window['gap_seconds']:g}s")
        lines.append(f"{edge['source']} --[" + ";".join(concise) + f"]-- {edge['target']}")
    lines.extend(
        f"{parent} ->contains-> {','.join(children)}" for parent, children in contains.items()
    )
    context = "\n".join(lines) + "\n\n[Evidence]\n" + _render(selected, "compact")
    bindings = {
        f"C{i + 1}": [
            n["ref"]
            for n in nodes
            if n["kind"] != "navigation_only" and f"C{i + 1}" in n["reason"].get("clauses", [])
        ]
        for i in range(len(plan.program["facets"]))
    }
    navigation_bindings = {
        f"C{i + 1}": [
            n["ref"]
            for n in nodes
            if n["kind"] == "navigation_only" and f"C{i + 1}" in n["reason"].get("clauses", [])
        ]
        for i in range(len(plan.program["facets"]))
    }
    rendered_paths = [
        {
            "target": refs[target],
            "steps": [
                {
                    "source": refs[a],
                    "target": refs[b],
                    "kind": edge["kind"],
                    "edge_id": edge.get("edge_id"),
                    "direction": direction,
                }
                for a, b, edge, direction in steps
            ],
        }
        for target, steps in paths.items()
    ]
    return context, {
        "nodes": nodes,
        "edges": edges,
        "query_plan": plan.program,
        "navigation_sources": navigation_sources,
        "text_relation_edges": text_relations,
        "paths": rendered_paths,
        "navigation_clause_bindings": navigation_bindings,
        "clause_bindings": bindings,
        "unmatched_clauses": [key for key, refs in bindings.items() if not refs],
    }


def pack_graph(plan, packer, *, max_tokens, top_k, summary_tokens):
    """Fill the bounded context, then repair missing matched-event representation."""
    options = {"max_tokens": max_tokens, "top_k": top_k, "summary_tokens": summary_tokens}
    if plan.refill_candidates:
        primary = [
            item for item in plan.candidates if node_key(item.unit) not in plan.refill_candidates
        ]
        keys = {node_key(item.unit) for item in primary}
        prior = replace(
            plan,
            candidates=primary,
            refill_candidates=set(),
            links=[e for e in plan.links if e["source"] in keys and e["target"] in keys],
        )
        selected, original = _pack_primary_graph(prior, packer, **options)
        extra = [
            item
            for item in plan.candidates
            if item.unit.hierarchy.role is HierarchyRole.SNAPSHOT
            and node_key(item.unit) in plan.refill_candidates
        ]
        extended, bundle = _pack_ordered_graph(plan, packer, extra, initial=selected, **options)
        if len(extended) == len(selected):
            return selected, replace(original, omitted_count=len(plan.candidates) - len(selected))
        bundle.navigation.update(
            event_reference_coverage={
                **original.navigation["event_reference_coverage"],
                "scope": "matched_references_in_primary_branches",
            },
            refill_sources_returned=len(extended) - len(selected),
        )
        return extended, bundle
    return _pack_primary_graph(plan, packer, **options)


def _pack_primary_graph(plan, packer, *, max_tokens, top_k, summary_tokens):
    sources = [c for c in plan.candidates if c.unit.hierarchy.role is HierarchyRole.SNAPSHOT]
    options = {"max_tokens": max_tokens, "top_k": top_k, "summary_tokens": summary_tokens}
    selected, bundle = _pack_ordered_graph(plan, packer, sources, **options)
    raw_keys = {node_key(c.unit) for c in sources}
    refs = {key: set() for key in raw_keys}
    for edge in plan.links:
        if edge["kind"] == "contains" and edge["target"] in refs:
            refs[edge["target"]].update(
                (edge["source"], ref["event_id"])
                for ref in edge.get("reason", {}).get("event_references", [])
            )
    required = set().union(*refs.values()) if refs else set()

    def represented(items):
        return set().union(*(refs.get(node_key(c.unit), set()) for c in items)) if items else set()

    covered = represented(selected)
    before, changed = len(covered), False
    if required - covered:
        # A second ordering is only useful when an actually unmatched event is
        # represented by a verified source in this same pool. Do not promote
        # redundant event mentions merely because they carry a source reference.
        preferred = sorted(sources, key=lambda c: not bool(refs[node_key(c.unit)]))
        alternative, packed = _pack_ordered_graph(plan, packer, preferred, **options)
        after = represented(alternative)
        if covered < after:
            selected, bundle, covered, changed = alternative, packed, after, True
    bundle.navigation["event_reference_coverage"] = {
        "available": len(required),
        "represented": len(covered),
        "before": before,
        "missing": len(required - covered),
        "refined": changed,
        "scope": "matched_direct_child_references_in_bounded_pool",
    }
    return selected, bundle


def _pack_ordered_graph(plan, packer, sources, *, max_tokens, top_k, summary_tokens, initial=None):
    summaries = [c for c in plan.candidates if c.unit.hierarchy.role is not HierarchyRole.SNAPSHOT]
    # Favor summaries that connect selected relation paths, then query support.
    linked = {
        key for e in plan.links if e["kind"] == "related" for key in (e["source"], e["target"])
    }
    summaries.sort(
        key=lambda c: (
            node_key(c.unit) not in linked,
            -plan.reasons[node_key(c.unit)].get("score", 0),
        )
    )
    selected = [] if initial is None else list(initial)
    context, graph = render_graph(selected, plan)
    tokens = packer._count(context) if selected else 0
    summary_used = 0
    summary_count = sum(c.unit.hierarchy.role is not HierarchyRole.SNAPSHOT for c in selected)
    for item in [*summaries, *sources] if initial is None else sources:
        if len(selected) >= top_k:
            break
        summary = item.unit.hierarchy.role is not HierarchyRole.SNAPSHOT
        if summary and sources:
            cost = packer._count(_render([item], "compact"))
            if summary_count >= packer.max_summaries or summary_used + cost > min(
                summary_tokens, max_tokens // 3
            ):
                continue
        else:
            cost = 0
        ordered = graph_order([*selected, item], plan)
        trial, trial_graph = render_graph(ordered, plan)
        size = packer._count(trial)
        if size > max_tokens:
            continue
        selected, context, graph, tokens = ordered, trial, trial_graph, size
        if summary:
            summary_count += 1
            summary_used += cost
    return selected, EvidenceBundle(
        rendered_context=context,
        sources=[
            UnitEvidenceRef(
                deepcopy(c.unit.scope),
                c.unit.id,
                hashlib.sha256(c.unit.content.encode()).hexdigest(),
            )
            for c in selected
        ],
        token_count=tokens,
        token_budget=max_tokens,
        tokenizer_id=packer.tokenizer_id,
        mode="relational",
        omitted_count=len(plan.candidates) - len(selected),
        navigation={
            "query_plan": plan.program,
            "evidence_graph": graph,
            "summary_nodes_returned": summary_count,
        },
    )
