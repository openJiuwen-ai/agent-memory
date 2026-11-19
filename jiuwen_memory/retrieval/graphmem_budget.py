"""Stage evidence tokens over one bounded graph; no extra reads or model calls."""

from __future__ import annotations

import re
from dataclasses import replace

from jiuwen_memory.common.graphmem_semantics import canonical
from jiuwen_memory.common.type_def import HierarchyRole
from jiuwen_memory.retrieval.expander import node_key
from jiuwen_memory.retrieval.graph_source_selection import is_personal_memory_query
from jiuwen_memory.retrieval.graphmem_query import needs_for, rule_needs, terms
from jiuwen_memory.retrieval.graphmem_relation_plan import clause_specs, mentions, scene_terms

_SOURCE_SUPPORT = 0.75


def simple_evidence_query(question, needs):
    """Complex/time/media requests keep the configured full evidence budget."""
    return bool(
        needs.mode == rule_needs(question).mode == "fact"
        and 1 <= len(needs.facets) <= 2
        and not (needs.time_from or needs.time_to or needs.analysis_status == "fallback")
        and not re.search(
            r"\b(?:photos?|photographs?|pictures?|images?|videos?|screenshots?|"
            r"january|february|march|april|may|june|july|august|september|october|"
            r"november|december|yesterday|today|tomorrow|last|next)\b|"
            r"照片|图片|图像|视频|截图|昨天|今天|明天", question, re.I))


def source_support(unit, needs, personal):
    """Sentence-local source signals, not parent event claims or answer confidence."""
    speaker = unit.user_metadata.get("speaker", "")
    speaker = speaker if isinstance(speaker, str) else ""
    scores = [0.0] * len(needs.facets)
    if personal and canonical(speaker) == "assistant":
        return scores
    text = re.sub(r"^\[Session [^\]\n]+\]\s*[^:\n]+:\s*", "", unit.content, count=1)
    sentences = [s for s in re.split(r"(?<=[.!?。！？])\s+|\n", text)
                 if s.strip() and not s.rstrip().endswith(("?", "？"))]
    for i, (clause, wanted) in enumerate(zip(clause_specs(needs), scene_terms(needs))):
        if not wanted:
            continue
        for sentence in sentences:
            attributed_first_person = bool(re.match(r"\s*I\b", sentence, re.I))
            if not all(mentions(sentence, name) or (
                    attributed_first_person and canonical(name) == canonical(speaker))
                       for name in clause["entities"]):
                continue
            scores[i] = max(scores[i], len(wanted & terms(sentence)) / len(wanted))
    return scores


def complete_source_parts(selected):
    """Do not declare saturation with an incomplete selected source payload."""
    fragments = {}
    for item in selected:
        unit = item.unit
        if unit.hierarchy.role is not HierarchyRole.SNAPSHOT:
            continue
        part, total = (unit.system_metadata.get(k) for k in ("_part", "_parts"))
        if part is None and total is None:
            continue
        if (type(part) is not int or type(total) is not int or not 1 <= part <= total
                or not unit.source_ref):
            return False
        key = (*node_key(unit)[:-1], unit.source_ref)
        previous, parts = fragments.setdefault(key, (total, set()))
        if previous != total:
            return False
        parts.add(part)
    return all(len(parts) == total for total, parts in fragments.values())


def bounded_support(plan, selected, signals):
    chosen = {node_key(c.unit) for c in selected}
    count = len(plan.program["facets"])
    coverage = [max((scores[i] for key, scores in signals.items() if key in chosen),
                    default=0.0) for i in range(count)]
    remaining = {key for key, scores in signals.items() if key not in chosen and (
        any(score >= _SOURCE_SUPPORT for score in scores)
        or bool(plan.reasons.get(key, {}).get("parent_event_references")))}
    complete = complete_source_parts(selected)
    return {"facet_coverage": coverage, "remaining_signal_sources": len(remaining),
            "selected_parts_complete": complete,
            "minimum_source_support": _SOURCE_SUPPORT,
            "covered": bool(coverage) and all(s >= _SOURCE_SUPPORT for s in coverage)
            and not remaining and complete}


def pack_adaptive_graph(plan, packer, policy, query, *, max_tokens, top_k):
    from .graphmem_evidence_graph import pack_graph

    needs = replace(needs_for(query), entities=tuple(plan.program["entities"]),
                    facets=tuple(plan.program["facets"]))
    ceiling = max_tokens
    tier = "full"
    tiered = policy.include_original and 0 < policy.adaptive_target_tokens < max_tokens
    if tiered:
        target = policy.adaptive_target_tokens
        facets = len(needs.facets)
        if needs.mode in {"comparison", "multi_step"} or facets >= 3:
            tier = "multi_evidence"
        elif needs.mode == "collection" or facets == 2:
            tier, ceiling = "expanded", target + (max_tokens - target) // 4
        else:
            tier, ceiling = "standard", target
    eligible = policy.include_original and simple_evidence_query(query.raw, needs)
    personal = is_personal_memory_query(query.raw)
    signals = {node_key(c.unit): source_support(c.unit, needs, personal)
               for c in plan.candidates if c.unit.hierarchy.role is HierarchyRole.SNAPSHOT}
    budget = min(ceiling, policy.initial_tokens) if eligible else ceiling
    limited = any(plan.diagnostics.get(k, False) for k in (
        "request_read_limited", "source_scope_limited"))
    stages = []
    while True:
        selected, bundle = pack_graph(plan, packer, max_tokens=budget, top_k=top_k,
                                      summary_tokens=policy.summary_token_budget)
        support = bounded_support(plan, selected, signals)
        early = eligible and support["covered"] and not limited and budget < ceiling
        stages.append({"budget": budget, "tokens": bundle.token_count,
                       "selected": len(selected), "signal_covered": support["covered"]})
        if early or budget == ceiling:
            bundle.navigation.update(stages=stages, adaptive_eligible=eligible,
                query_coverage=support, coverage_scope="signals_in_bounded_materialized_pool",
                stopping_reason="bounded_signals_saturated" if early else "budget_limit")
            if tiered:
                bundle.navigation.update(evidence_budget_tier=tier, tier_token_limit=ceiling)
            return selected, bundle
        budget = min(ceiling, budget * 2)
