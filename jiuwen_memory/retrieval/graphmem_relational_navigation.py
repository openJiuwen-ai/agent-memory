"""Query-conditioned, bounded graph search with explicit paths and original evidence."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace

from jiuwen_memory.common.errors import StorageRetrievalError, ValidationError
from jiuwen_memory.common.graph_memory import RELATION_KIND, memory_fingerprint
from jiuwen_memory.common.graphmem_semantics import decode_semantics
from jiuwen_memory.common.graphmem_structure import is_recursive, valid_child_role
from jiuwen_memory.common.time_utils import as_utc
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterOp,
    HierarchyRole,
    RecallChannel,
    ScoredMemoryUnit,
    and_merge,
    is_retrieval_candidate,
)
from jiuwen_memory.common.type_def.hierarchy_query import HierarchyQuery
from jiuwen_memory.retrieval.expander import node_key, within_scope
from jiuwen_memory.retrieval.graph_navigation import _terms
from jiuwen_memory.retrieval.graph_source_selection import (
    hybrid_order,
    is_personal_memory_query,
    personal_source_order,
)
from jiuwen_memory.retrieval.graphmem_chunk_hints import child_chunk_hints
from jiuwen_memory.retrieval.graphmem_query import is_named_entity, needs_for, terms
from jiuwen_memory.retrieval.graphmem_relation_plan import (
    candidate_limits,
    child_detail_hints,
    child_event_hints,
    clause_specs,
    compile_program,
    detail_vector_leader,
    entity_matches,
    event_signals,
    ground_query_entities,
    mentions,
    node_signals,
)
from jiuwen_memory.retrieval.graphmem_relation_selection import (
    edge_hint,
    relation_reason,
)
from jiuwen_memory.retrieval.graphmem_request_reads import _RequestReads
from jiuwen_memory.retrieval.retriever_impl.predicate_builder import (
    build_hierarchy_filters,
    build_system_filters,
)

_EXTRA_STOP = frozenset("i me my mine you your have has had been can could would should "
                        "about tell please session summary user assistant".split())


def _query_terms(text, normalize=False):
    return terms(text) if normalize else _terms(text) - _EXTRA_STOP


def _linked(parent, child):
    if (parent.hierarchy.kind != child.hierarchy.kind
            or not valid_child_role(parent, child)
            or child.hierarchy.parent_id != parent.id
            or child.hierarchy.resolved_parent_scope(child.scope) != parent.scope):
        return False
    if not any(uid == child.id and parent.hierarchy.child_scope_at(i, parent.scope) == child.scope
               for i, uid in enumerate(parent.hierarchy.child_ids)):
        return False
    p, c = parent.hierarchy, child.hierarchy
    return bool(p.span_start and p.span_end and c.span_start and c.span_end
                and as_utc(p.span_start) <= as_utc(c.span_start)
                and as_utc(c.span_end) <= as_utc(p.span_end))


@dataclass
class RelationalEvidencePlan:
    candidates: list = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    reasons: dict = field(default_factory=dict)
    program: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    refill_candidates: set = field(default_factory=set)


def _descendant_probe_reserve(summaries, known, scope, branch_width, room):
    """Count unread direct summary children from already selected parent metadata."""
    seen, reserved = set(known), 0
    for item in summaries:
        parent = item.unit
        kinds = parent.system_metadata.get("graphmem_child_roles", [])
        if (
            not is_recursive(parent)
            or not isinstance(kinds, list)
            or len(kinds) != len(parent.hierarchy.child_ids)
            or any(
                k not in {HierarchyRole.SNAPSHOT.value, HierarchyRole.TIME_SPAN.value}
                for k in kinds
            )
        ):
            continue
        groups = defaultdict(set)
        for i, uid in enumerate(parent.hierarchy.child_ids):
            if kinds[i] != HierarchyRole.TIME_SPAN.value:
                continue
            child_scope = parent.hierarchy.child_scope_at(i, parent.scope)
            key = _RequestReads.key(child_scope, uid)
            if within_scope(child_scope, scope) and key not in seen:
                groups[key[:-1]].add(key)
        for keys in groups.values():
            reserved += min(branch_width, len(keys - seen))
            seen.update(keys)
        if reserved >= room:
            return room
    return reserved


def _index_candidates(
    cache,
    query,
    target_scope,
    ids,
    role,
    limit,
    needs,
    expand_terms=False,
    strict_pool=False,
    keyword_text=None,
    *,
    entry_events=None,
    entry_vector_ranks=None,
):
    """Select IDs using existing scoped indexes before reading candidate bodies."""
    indexed_query = replace(query, hierarchy_role=role)
    if entry_events is not None:
        indexed_query.recall_entry_semantics = True
    if expand_terms and needs.analysis_status in {"used", "used_rules_guard"}:
        indexed_query.rewritten = (query.rewritten or query.raw) + " " + " ".join(needs.facets)
    indexed_query.scalar_filters = and_merge(
        query.recheck_filters,
        [
            FilterClause("unit_id", FilterOp.IN, ids),
            *build_hierarchy_filters(HierarchyQuery.from_query(indexed_query)),
            *build_system_filters(
                query.as_of, query.time_from, query.time_to, query.include_archived
            ),
        ],
    )
    channels = [RecallChannel.VECTOR, RecallChannel.KEYWORD]
    if strict_pool:
        # Split an odd quota explicitly; flooring the same per-channel limit
        # left a spare slot that was previously filled by an unranked child.
        allocations = (
            [(RecallChannel.VECTOR, (limit + 1) // 2), (RecallChannel.KEYWORD, limit // 2)]
            if query.vector
            else [(RecallChannel.KEYWORD, limit)]
        )
        keyword_query = (
            replace(indexed_query, rewritten=keyword_text) if keyword_text else indexed_query
        )
        results = [
            cache.recall(
                target_scope,
                keyword_query if channel is RecallChannel.KEYWORD else indexed_query,
                channels=[channel],
                recall_limit=quota,
            )
            for channel, quota in allocations
            if quota
        ]
    else:
        results = [cache.recall(target_scope, indexed_query, channels=channels, recall_limit=limit)]
    per_channel, vectors = {}, {}
    for result in results:
        if result.errors:
            raise StorageRetrievalError(result.errors)
        for batch in result.batches:
            for rank, item in enumerate(batch.candidates, 1):
                if item.unit_id not in ids or not math.isfinite(item.score):
                    continue
                if entry_events is not None and item.entry_semantics is not None:
                    key = item.unit_id
                    previous = entry_events.setdefault(key, item.entry_semantics)
                    if previous != item.entry_semantics:
                        entry_events[key] = None
                if (
                    entry_vector_ranks is not None
                    and batch.channel is RecallChannel.VECTOR
                    and batch.source in {"vector_l2", "vector_l1"}
                    and item.score > 0
                ):
                    ranks = entry_vector_ranks.setdefault(item.unit_id, {})
                    ranks[batch.source] = min(ranks.get(batch.source, rank), rank)
                key = item.unit_id, batch.channel
                per_channel[key] = max(per_channel.get(key, 0), 1 / (60 + rank))
                if batch.channel is RecallChannel.VECTOR:
                    vectors[item.unit_id] = max(vectors.get(item.unit_id, 0), item.score)
    scores = defaultdict(float)
    for (uid, _), value in per_channel.items():
        scores[uid] += value
    leader = (
        detail_vector_leader([b for r in results for b in r.batches], ids)
        if role is not HierarchyRole.SNAPSHOT
        else None
    )
    return scores, vectors, leader


def _uncovered_probe(entry_events, ordered_ids, needed, needs):
    """Prefer an indexed, attributed event for a missing clause before point reads."""
    best, priority = None, (0, 0.0)
    for uid in ordered_ids:
        encoded = entry_events.get(uid)
        if encoded is None:
            continue
        try:
            features = decode_semantics(encoded)
        except ValidationError:
            continue
        if features is None:
            continue
        supported = {}
        for event in features.events:
            if not event.sources:
                continue
            signals = event_signals(features, event, needs)
            expected = "match" if needs.time_from or needs.time_to else None
            if expected is not None and signals["window"] != expected:
                continue
            for hit in signals["matches"]:
                if hit["clause"] not in needed or hit["scene_score"] < 2 / 3:
                    continue
                if needed[hit["clause"]] and not hit["entities"]:
                    continue
                supported[hit["clause"]] = max(supported.get(hit["clause"], 0), hit["scene_score"])
        score = len(supported), sum(supported.values())
        if score > priority:
            best, priority = uid, score
    return best


def _semantic_endpoint(entry_events, vector_ranks, ordered, missing, needs, semantic_ids):
    """A bounded vector suggestion can route a probe, without grounding a clause."""
    shortlist = []
    for index, uid in enumerate(ordered):
        ranks = vector_ranks.get(uid, {})
        best = min(ranks.values(), default=4)
        if best > 3 or uid not in semantic_ids or entry_events.get(uid) is None:
            continue
        try:
            features = decode_semantics(entry_events[uid])
        except ValidationError:
            continue
        if features is None:
            continue
        for event in features.events:
            if not event.sources:
                continue
            attributed = any(
                names and entity_matches(features, names, event.entity_keys)
                for names in missing.values()
            )
            windowed = bool(needs.time_from or needs.time_to)
            timely = windowed and event_signals(features, event, needs)["window"] == "match"
            if (
                attributed
                and (not windowed or timely)
                or timely
                and any(not names for names in missing.values())
            ):
                shortlist.append((best, -sum(rank <= 3 for rank in ranks.values()), index, uid))
                break
    return min(shortlist)[-1] if shortlist else None


def _source_clause_query(needs, support):
    """Use one branch's confirmed event scope for its existing lexical channel."""
    clauses = {
        hit["clause"]
        for event in support["event_bindings"]
        if event["window"] != "outside"
        and (not (needs.time_from or needs.time_to) or event["window"] == "match")
        for hit in event["matches"]
    }
    if len(clauses) != 1:
        return None
    for spec in clause_specs(needs):
        if spec["id"] in clauses:
            return " ".join(
                (
                    spec["scene"],
                    *[
                        name
                        for name in spec["entities"]
                        if name.casefold() not in spec["scene"].casefold()
                    ],
                )
            )
    return None


def _spare_chunk_sources(cache, query, parent, child_scope, ids, chosen, quota, needs, policy):
    """Use validated summary locations only for empty slots after native selection."""
    room = max(0, quota - len(chosen))
    if not room:
        return [], {}, {}, {}
    remaining = [uid for uid in ids if uid not in chosen]
    hints = child_chunk_hints(
        parent,
        cache.content_matches.get(node_key(parent)),
        child_scope,
        remaining,
        HierarchyRole.SNAPSHOT,
    )
    if not hints:
        return [], {}, {}, {}
    scores, vectors, _ = _index_candidates(
        cache,
        query,
        child_scope,
        list(hints),
        HierarchyRole.SNAPSHOT,
        room,
        needs,
        policy.semantic_source_terms,
        strict_pool=True,
    )
    # Hint order is only navigation priority. Index, parent-link and visibility
    # checks remain mandatory before any source can enter the evidence pool.
    extra = [uid for uid in hints if uid in scores][:room]
    return (
        extra,
        {uid: scores[uid] for uid in extra},
        {uid: vectors[uid] for uid in extra if uid in vectors},
        {uid: hints[uid] for uid in extra},
    )


def _indexed_edge_source(cache, query, scope, parent, needs):
    """Require the same top hit in endpoint-scoped native source views."""
    kinds = parent.system_metadata.get("graphmem_child_roles", [])
    children = parent.hierarchy.child_ids
    if (
        not is_recursive(parent)
        or len(kinds) != len(children)
        or not children
        or any(k != HierarchyRole.SNAPSHOT.value for k in kinds)
    ):
        return None
    child_scope = parent.hierarchy.child_scope_at(0, parent.scope)
    if not within_scope(child_scope, scope) or any(
        parent.hierarchy.child_scope_at(i, parent.scope) != child_scope
        for i in range(len(children))
    ):
        return None
    role = HierarchyRole.SNAPSHOT
    index_query = replace(query, hierarchy_role=role, recall_entry_semantics=True)
    index_query.scalar_filters = and_merge(
        query.recheck_filters,
        [
            FilterClause("unit_id", FilterOp.IN, children),
            *build_hierarchy_filters(HierarchyQuery.from_query(index_query)),
            *build_system_filters(
                query.as_of, query.time_from, query.time_to, query.include_archived
            ),
        ],
    )
    results = cache.recall(
        child_scope,
        index_query,
        channels=[RecallChannel.KEYWORD, RecallChannel.VECTOR],
        recall_limit=2,
    )
    if results.errors:
        return None
    heads = {
        batch.source: batch.candidates[0]
        for batch in results.batches
        if batch.candidates
        and (batch.channel, batch.source)
        in {(RecallChannel.KEYWORD, "keyword_l2"), (RecallChannel.VECTOR, "vector_l2")}
    }
    lexical, vector = heads.get("keyword_l2"), heads.get("vector_l2")
    if (
        lexical is None
        or vector is None
        or lexical.unit_id != vector.unit_id
        or lexical.unit_id not in children
        or any(not math.isfinite(h.score) or h.score <= 0 for h in (lexical, vector))
    ):
        return None
    payload = re.sub(r"^\[Session [^\]]*\]\s*", "", lexical.entry_keyword_text)
    speaker, sep, body = payload.partition(":")
    if not sep or len(speaker.split()) > 6:
        speaker, body = "", payload
    for clause in clause_specs(needs):
        names = [name for name in clause["entities"] if is_named_entity(name)]
        if not names:
            continue
        words = terms(clause["scene"]) - terms(" ".join(names))
        if not words:
            continue
        for sentence in re.split(r"[.!?。！？;；\n]+", body):
            if all(mentions(speaker + " " + sentence, name) for name in names) and len(
                terms(sentence) & words
            ) >= math.ceil(len(words) / 3):
                return child_scope, lexical.unit_id, vector.score
    return None


def _swap_unranked_adjacent(groups):
    """Pick at most one optional unranked neighbor, preserving its branch seed."""
    for group in reversed(groups):
        _, _, ids, scores, _, hints, details, _ = group
        if len(ids) < 2:
            continue
        for uid in reversed(ids):
            if uid not in scores and uid not in hints and uid not in details and uid != ids[0]:
                return ids, uid
    return None


def plan_relational(
    domain, policy, scope, query, candidates, *, limits=None, entry_pool=None,
    recall_channels=None,
):
    limits = limits or candidate_limits(domain, policy, scope, query)
    summary_limit, source_limit = limits["summary_limit"], limits["source_limit"]
    candidates = candidates[: limits["initial_limit"]]
    needs = ground_query_entities(query, candidates)
    program = compile_program(query, needs)
    cache = _RequestReads(domain, policy, candidates, recall_channels=recall_channels)
    plan = RelationalEvidencePlan(
        program=program.describe(),
        diagnostics={
            "initial_candidates": len(candidates),
            "summary_candidates": 0,
            "source_candidates": 0,
            "neighbors_added": 0,
            "stale_edges": 0,
            "filtered": 0,
            "index_fallbacks": 0,
            "candidate_limit_reached": False,
            "source_vector_matches": 0,
            "source_index_ids_returned": 0,
            "edge_metadata_rejected": 0,
            "edge_conditions_rejected": 0,
            "semantic_probes_added": 0,
            "empty_index_branches": 0,
            "event_reference_index_hits": 0,
            "event_source_candidates": 0,
            **limits,
        },
    )
    if policy.conversation_day_lookup and query.span_start is not None:
        from jiuwen_memory.retrieval.graphmem_query import conversation_day_span

        if conversation_day_span(query) == (query.span_start, query.span_end):
            plan.diagnostics["conversation_day_lookup"] = True
    if policy.detail_source_hints or policy.chunk_source_hints:
        plan.diagnostics.update(detail_reference_index_hits=0, detail_source_candidates=0)
    if policy.chunk_source_hints:
        plan.diagnostics.update(chunk_reference_index_hits=0, chunk_source_candidates=0)
    parsed = replace(query, hierarchy_role=None)
    summaries, originals, signals = {}, {}, {}
    summary_reads = {node_key(c.unit) for c in candidates}
    reserved_entry = None
    reserved_key = None
    coarse_keyword_preferred = False
    temporal_keyword_preferred = False
    if policy.refill_entry_probes and entry_pool is not None:
        if (
            entry_pool.scope != scope
            or len(entry_pool.initial_ids) > limits["initial_limit"]
            or len(entry_pool.initial_ids) + len(entry_pool.alternative_ids) > summary_limit
            or (
                entry_pool.corroborated_alternative is not None
                and entry_pool.corroborated_alternative not in entry_pool.alternative_ids
            )
        ):
            raise ValidationError("entry probe pool must match the request scope and limits")
        # Initial get attempts count even if their source was missing or filtered.
        summary_reads.update(cache.key(scope, uid) for uid in entry_pool.initial_ids)
        cache.attempted.update(cache.key(scope, uid) for uid in entry_pool.initial_ids)
        plan.diagnostics.update(
            entry_alternatives=len(entry_pool.alternative_ids),
            entry_refill_probes=0,
            entry_refill_added=0,
        )
        if entry_pool.attributed_entry in entry_pool.initial_ids:
            plan.diagnostics["attributed_entry_selected"] = True
        if policy.reserve_corroborated_entry:
            reserved_entry = entry_pool.corroborated_alternative
            if (
                entry_pool.alternative_ids
                and 2 <= summary_limit - limits["initial_limit"] <= 4
                and len(program.entities) == len(program.facets) == 1
            ):
                coarse = entry_pool.coarse_keyword_alternative
                temporal = entry_pool.temporal_keyword_alternative
                if policy.prefer_coarse_keyword_entry and coarse:
                    reserved_entry = coarse
                    coarse_keyword_preferred = True
                    plan.diagnostics["coarse_keyword_entry_reserved"] = True
                elif (
                    policy.prefer_temporal_keyword_entry and temporal and program.mode == "temporal"
                ):
                    reserved_entry = temporal
                    temporal_keyword_preferred = True
                    plan.diagnostics["temporal_keyword_entry_reserved"] = True
            if policy.reserve_temporal_anchor_entry and entry_pool.temporal_anchor_alternative:
                reserved_entry = entry_pool.temporal_anchor_alternative
                coarse_keyword_preferred = temporal_keyword_preferred = False
                plan.diagnostics["temporal_anchor_entry_reserved"] = True
            alternatives = entry_pool.alternative_ids
            if reserved_entry is not None and reserved_entry not in alternatives:
                entry_pool = replace(
                    entry_pool, alternative_ids=(*alternatives[:-1], reserved_entry)
                )
        if reserved_entry is not None:
            reserved_key = cache.key(scope, reserved_entry)
            plan.diagnostics["corroborated_entry_reserved"] = True

    def summary_room():
        return max(
            0,
            summary_limit
            - len(summary_reads)
            - int(reserved_key is not None and reserved_key not in summary_reads),
        )

    def visible(unit, role=None):
        return within_scope(unit.scope, scope) and is_retrieval_candidate(
            unit, replace(parsed, hierarchy_role=role), filters=query.recheck_filters
        )

    def read(target_scope, ids, *, summary=False):
        if not within_scope(target_scope, scope):
            plan.diagnostics["filtered"] += len(ids)
            return {}
        if summary:
            allowed = []
            for uid in ids:
                key = cache.key(target_scope, uid)
                if (
                    key in summary_reads
                    or summary_room()
                    or (key == reserved_key and len(summary_reads) < summary_limit)
                ):
                    summary_reads.add(key)
                    allowed.append(uid)
                else:
                    plan.diagnostics["candidate_limit_reached"] = True
            ids = allowed
        return {u.id: u for u in cache.get(target_scope, ids) if visible(u)}

    def signal(unit):
        key = node_key(unit)
        if key not in signals:
            signals[key] = node_signals(unit, needs)
        return signals[key]

    def add_summary(item, why):
        key = node_key(item.unit)
        if key in summaries:
            return True
        if len(summaries) >= summary_limit:
            plan.diagnostics["candidate_limit_reached"] = True
            return False
        if not visible(item.unit) or item.unit.hierarchy.role is HierarchyRole.SNAPSHOT:
            return False
        summaries[key] = item
        plan.reasons[key] = {"selected_by": why, **signal(item.unit)}
        return True

    # Preserve vector/keyword entry quality while spreading entry slots over query clauses.
    remaining = list(enumerate(candidates))
    covered, seeds = set(), []
    while remaining and len(seeds) < policy.seed_count:
        best = max(
            range(len(remaining)),
            key=lambda i: (
                len(set(signal(remaining[i][1].unit)["clauses"]) - covered),
                signal(remaining[i][1].unit)["score"] + 1 / (1 + remaining[i][0]),
            ),
        )
        _, item = remaining.pop(best)
        if add_summary(item, "initial_vector_keyword_match"):
            seeds.append(item)
            covered.update(signal(item.unit)["clauses"])
    frontier = list(seeds)
    for seed in seeds:
        child, seen = seed.unit, {node_key(seed.unit)}
        for _ in range(policy.ancestor_hops if policy.use_relations else 0):
            if not child.hierarchy.parent_id:
                break
            ps = child.hierarchy.resolved_parent_scope(child.scope)
            parent = read(ps, [child.hierarchy.parent_id], summary=True).get(
                child.hierarchy.parent_id
            )
            if parent is None or node_key(parent) in seen or not _linked(parent, child):
                break
            seen.add(node_key(parent))
            item = ScoredMemoryUnit(parent, seed.score, RecallChannel.HIERARCHY)
            if not add_summary(item, "verified_parent_for_relation_navigation"):
                break
            plan.links.append(
                {
                    "source": node_key(parent),
                    "target": node_key(child),
                    "kind": "contains",
                    "reason": {"verified_child_link": True},
                }
            )
            frontier.append(item)
            child = parent

    seen_edges = set()
    probed_endpoints = {}
    for hop in range(policy.max_hops if policy.use_relations else 0):
        active = {c.unit.id: c for c in frontier if c.unit.scope == scope}
        if not active or plan.diagnostics["neighbors_added"] >= policy.max_neighbors:
            break
        edges = cache.related_edges(
            scope, list(active), relation=RELATION_KIND, limit=policy.max_edges
        )
        probes = []
        for edge in edges:
            if edge.id in seen_edges:
                continue
            seen_edges.add(edge.id)
            start = edge.source if edge.source in active else edge.target
            target = edge.target if start == edge.source else edge.source
            attrs = set(edge.properties.get("attributes", ())) & set(program.relations)
            strength = edge.properties.get("score", 0)
            if start not in active or not attrs or type(strength) not in (int, float):
                continue
            if not math.isfinite(strength):
                continue
            hint = edge_hint(active[start].unit, edge, needs, program)
            if hint is None:
                plan.diagnostics["edge_metadata_rejected"] += 1
                continue
            probes.append((strength, edge.id, edge, start, target, hint))
        target_ids = list(dict.fromkeys(t for _, _, _, _, t, _ in probes))
        if not target_ids:
            break
        allow_named_probe = (
            policy.semantic_endpoint_probe == "named"
            and len(program.facets) == len(needs.entities) == 1
        )
        missing_clauses = (
            {c["id"]: c["entities"] for c in clause_specs(needs) if c["id"] not in covered}
            if policy.probe_uncovered_clauses and (len(program.facets) > 1 or allow_named_probe)
            else {}
        )
        entry_events, entry_vector_ranks = {}, {}
        semantic_possible = (
            policy.semantic_endpoint_probe != "off"
            and not plan.diagnostics.get("native_semantic_endpoint_promoted")
            and bool(missing_clauses)
            and (
                bool(needs.time_from or needs.time_to)
                or ((bool(covered) or allow_named_probe) and any(missing_clauses.values()))
            )
        )
        retrieval_metadata = {"entry_events": entry_events} if missing_clauses else {}
        if semantic_possible:
            retrieval_metadata["entry_vector_ranks"] = entry_vector_ranks
        rank_scores, query_vectors, detail_leader = _index_candidates(
            cache,
            query,
            scope,
            target_ids,
            query.hierarchy_role,
            policy.max_neighbors * 3,
            needs,
            policy.semantic_source_terms,
            **retrieval_metadata,
        )
        probes = [p for p in probes if p[4] in rank_scores]
        probes.sort(
            key=lambda x: (
                x[4] != detail_leader,
                -rank_scores[x[4]],
                -bool(x[5]["shared_query_entities"]),
                -x[0],
                x[1],
            )
        )
        # Duplicate incident edges must not crowd out distinct endpoint IDs.
        # The budget is enforced before target body reads, including failed probes.
        known_before = set(summary_reads)
        room = summary_room()
        descendant_reserve = (
            _descendant_probe_reserve(
                summaries.values(), known_before, scope, policy.branch_width, room
            )
            if policy.reserve_descendant_probes
            else 0
        )
        room -= descendant_reserve
        stage_room = math.ceil(room / (policy.max_hops - hop))
        semantic_alternate = None
        if entry_events and stage_room >= 2:
            unread = list(
                dict.fromkeys(
                    target
                    for _, _, _, _, target, _ in probes
                    if cache.key(scope, target) not in known_before
                )
            )
            if len(unread) > stage_room:
                first, remaining = unread[:stage_room], unread[stage_room:]
                # Preserve the native head and the other ranked endpoints. Only
                # replace the last fresh probe when no admitted index candidate
                # supplies strong attributed support for the missing clause.
                if _uncovered_probe(entry_events, first, missing_clauses, needs) is None:
                    preferred = _uncovered_probe(entry_events, remaining, missing_clauses, needs)
                    if preferred is None and entry_vector_ranks:
                        semantic_ids = {
                            row[4] for row in probes if "semantic" in row[5]["attributes"]
                        }
                        semantic_alternate = _semantic_endpoint(
                            entry_events,
                            entry_vector_ranks,
                            remaining,
                            missing_clauses,
                            needs,
                            semantic_ids,
                        )
                        preferred = semantic_alternate
                    if preferred is not None:
                        current, moved = [], []
                        for probe in probes:
                            (moved if probe[4] == preferred else current).append(probe)
                        position = next(
                            i for i, probe in enumerate(current) if probe[4] == first[-1]
                        )
                        probes = [*current[:position], *moved, *current[position:]]
                        plan.diagnostics["uncovered_clause_probe_promoted"] = (
                            plan.diagnostics.get("uncovered_clause_probe_promoted", 0) + 1
                        )
                        if semantic_alternate is not None:
                            plan.diagnostics["native_semantic_endpoint_promoted"] = True

        def choose_targets(new_limit):
            selected, new = [], 0
            for _, _, _, _, target, _ in probes:
                if target in selected:
                    continue
                known = cache.key(scope, target) in known_before
                if not known and new >= new_limit:
                    continue
                selected.append(target)
                new += not known
                if len(selected) >= policy.max_neighbors * 3:
                    break
            return selected

        chosen = choose_targets(stage_room)
        # Collections may need another instance beyond a selected endpoint.
        # Other operators keep the original leading-endpoint eligibility.
        probe_target = chosen[0] if chosen else None
        if program.combine == "union":
            probe_target = next(
                (uid for uid in chosen if cache.key(scope, uid) not in summaries), None
            )
        options = []

        def probe_targets(ids):
            targets = read(scope, ids, summary=True)
            for strength, _, edge, start, target, hint in probes:
                if target not in ids:
                    continue
                left, right = active[start].unit, targets.get(target)
                if right is None or right.hierarchy.role is HierarchyRole.SNAPSHOT:
                    continue
                stamps = edge.properties.get("fingerprints", {})
                if (
                    not isinstance(stamps, dict)
                    or stamps.get(start) != memory_fingerprint(left)
                    or stamps.get(target) != memory_fingerprint(right)
                ):
                    plan.diagnostics["stale_edges"] += 1
                    continue
                similarity = query_vectors.get(target, 0)
                if policy.relation_source_consensus:
                    key = node_key(right)
                    prior = probed_endpoints.get(key)
                    weight = (similarity, strength, edge.id)
                    if prior is None or weight > prior[0]:
                        probed_endpoints[key] = (
                            weight,
                            ScoredMemoryUnit(right, active[start].score, RecallChannel.GRAPH),
                        )
                reason = relation_reason(
                    left,
                    right,
                    edge,
                    needs,
                    program,
                    similarity,
                    allow_semantic_probe=(
                        (
                            target == probe_target
                            or (target == semantic_alternate and "semantic" in hint["attributes"])
                        )
                        and node_key(right) not in summaries
                        and not plan.diagnostics["semantic_probes_added"]
                    ),
                    allow_entity_probe=policy.query_guided_entity_probe,
                )
                if not reason:
                    plan.diagnostics["edge_conditions_rejected"] += 1
                    continue
                if target == semantic_alternate and reason.get("semantic_probe"):
                    reason["ranked_vector_endpoint"] = True
                if edge.properties.get("stage") == "isolated_semantic_backstop":
                    # The relation can guide retrieval but may not order the reader's passages.
                    reason["navigation_only_backstop"] = True
                joins = (
                    program.combine == "join"
                    and signal(left)["clauses"]
                    and set(signal(right)["clauses"]) - set(signal(left)["clauses"])
                )
                if joins:
                    reason["join_clauses"] = {
                        "from": signal(left)["clauses"],
                        "to": signal(right)["clauses"],
                    }
                options.append(
                    (
                        bool(joins) + signal(right)["score"] + similarity + strength,
                        edge.id,
                        left,
                        right,
                        reason,
                        active[start].score,
                    )
                )

        probe_targets(chosen)
        # Keep a later breadth-first hop possible instead of spending all reads
        # on the first neighborhood. A dead end has no later hop to reserve for:
        # finish probing its original ranked pool, without changing eligibility.
        fallback = stage_room < room and not any(
            node_key(option[3]) not in summaries for option in options
        )
        if fallback:
            remaining_targets = [uid for uid in choose_targets(room) if uid not in chosen]
            probe_targets(remaining_targets)
        plan.diagnostics.setdefault("relation_probe_stages", []).append(
            {
                "hop": hop + 1,
                "new_probe_limit": stage_room,
                "remaining_before": room,
                "new_probes": len(summary_reads - known_before),
                "dead_end_fallback": fallback,
                **(
                    {
                        "descendant_probe_reserve": descendant_reserve,
                        "remaining_total_before": room + descendant_reserve,
                    }
                    if policy.reserve_descendant_probes
                    else {}
                ),
            }
        )
        frontier, degrees = [], defaultdict(int)
        while options:
            # Recompute marginal coverage after EVERY admission. Static gains
            # send all branch slots to repeated copies of the same first clause.
            best = min(
                range(len(options)),
                key=lambda i: (
                    bool(options[i][4].get("semantic_probe")),
                    -len(set(signal(options[i][3])["clauses"]) - covered),
                    -options[i][0],
                    options[i][1],
                ),
            )
            _, edge_id, left, right, reason, score = options.pop(best)
            if degrees[node_key(left)] >= policy.branch_width:
                continue
            key = node_key(right)
            if reason.get("semantic_probe") and (
                plan.diagnostics["semantic_probes_added"] or key in summaries
            ):
                continue
            reason["new_clauses"] = sorted(set(signal(right)["clauses"]) - covered)
            if key not in summaries:
                if plan.diagnostics["neighbors_added"] >= policy.max_neighbors:
                    break
                item = ScoredMemoryUnit(right, score, RecallChannel.GRAPH)
                if not add_summary(item, "query_relevant_relation"):
                    continue
                plan.diagnostics["neighbors_added"] += 1
                frontier.append(item)
                covered.update(signal(right)["clauses"])
            plan.diagnostics["semantic_probes_added"] += bool(reason.get("semantic_probe"))
            degrees[node_key(left)] += 1
            plan.links.append(
                {
                    "source": node_key(left),
                    "target": key,
                    "kind": "related",
                    "edge_id": edge_id,
                    "reason": reason,
                }
            )

    def indexed_children(parent, target_scope, ids, role, limit):
        event_hints = child_event_hints(parent, needs, target_scope, ids, role)
        detail_hints = (
            child_detail_hints(parent, needs, target_scope, ids, role)
            if policy.detail_source_hints and not event_hints
            else {}
        )
        hints = event_hints or detail_hints
        hinted, vectors = {}, {}
        strict = role is HierarchyRole.SNAPSHOT
        if hints:
            hinted, vectors, _ = _index_candidates(
                cache,
                query,
                target_scope,
                list(hints),
                role,
                max(1, limit // 2),
                needs,
                policy.semantic_source_terms,
                strict_pool=True,
            )
        room = limit - len(hinted)
        remaining_ids = [uid for uid in ids if uid not in hinted]
        votes = dict(hinted)
        if room and remaining_ids:
            keyword_text = None
            if (
                policy.source_clause_keywords
                and strict
                and (room > 1 or not query.vector)
                and len(needs.facets) > 1
                and needs.analysis_status in {"used", "used_rules_guard"}
            ):
                keyword_text = _source_clause_query(needs, signal(parent))
                if keyword_text:
                    plan.diagnostics["source_clause_index_calls"] = (
                        plan.diagnostics.get("source_clause_index_calls", 0) + 1
                    )
            other_votes, other_vectors, _ = _index_candidates(
                cache,
                query,
                target_scope,
                remaining_ids,
                role,
                room,
                needs,
                policy.semantic_source_terms,
                strict_pool=strict,
                **({"keyword_text": keyword_text} if keyword_text else {}),
            )
            votes.update(other_votes)
            vectors.update(other_vectors)
        if role is HierarchyRole.SNAPSHOT:
            plan.diagnostics["source_vector_matches"] += len(vectors)
            plan.diagnostics["source_index_ids_returned"] += len(votes)
        if not votes:
            plan.diagnostics["empty_index_branches"] += 1
        # Empty or overlapping channels do not authorize arbitrary prefix
        # children. Only indexed hits and explicit adjacent context can enter.
        order = [
            *sorted(hinted, key=lambda uid: list(hints).index(uid)),
            *sorted(
                (uid for uid in votes if uid not in hinted),
                key=lambda uid: (-votes[uid], ids.index(uid)),
            ),
        ]
        kept_hints = {uid: hints[uid] for uid in order[:limit] if uid in hinted}
        kept_events = {uid: hint for uid, hint in kept_hints.items() if uid in event_hints}
        kept_details = {uid: hint for uid, hint in kept_hints.items() if uid in detail_hints}
        plan.diagnostics["event_reference_index_hits"] += len(kept_events)
        if policy.detail_source_hints or policy.chunk_source_hints:
            plan.diagnostics["detail_reference_index_hits"] += len(kept_details)
        if policy.chunk_source_hints:
            plan.diagnostics["chunk_reference_index_hits"] += sum(
                "chunk_seq" in references[0] for references in kept_details.values()
            )
        return order[:limit], votes, vectors, kept_events, kept_details

    queue, expanded, source_groups = deque(summaries.values()), set(), []
    refill_source_start = None
    primary_summaries = set()
    # A speculative endpoint has not satisfied a query clause. It must not
    # suppress the remaining bounded index fallback as a qualified edge would.
    ordinary_refill = bool(
        policy.refill_entry_probes
        and policy.use_relations
        and entry_pool is not None
        and plan.diagnostics["neighbors_added"] <= plan.diagnostics["semantic_probes_added"]
    )
    refill_pending = ordinary_refill or (reserved_key is not None and reserved_key not in summaries)
    while queue or refill_pending:
        if not queue:
            # One bounded batch, only after the original trees have descended.
            # These IDs came from the original indexes, not a new bank scan.
            refill_pending = False
            refill_source_start = len(source_groups)
            primary_summaries = set(summaries)
            room = max(0, summary_limit - len(summary_reads))
            ids = []
            alternatives = entry_pool.alternative_ids if ordinary_refill else ()
            exact_alternative = entry_pool.deferred_detail_keyword
            if (
                policy.prefer_deferred_detail_keyword
                and alternatives
                and program.combine == "union"
                and exact_alternative is not None
            ):
                alternatives = (exact_alternative, *alternatives[:-1])
            if reserved_entry is not None:
                alternatives = tuple(dict.fromkeys((reserved_entry, *alternatives)))
            for uid in alternatives:
                key = cache.key(scope, uid)
                if key in summaries:
                    continue
                if key not in summary_reads:
                    if not room:
                        continue
                    room -= 1
                ids.append(uid)
                if len(ids) >= limits["initial_limit"]:
                    break
            before = len(summary_reads)
            loaded = read(scope, ids, summary=True)
            plan.diagnostics["entry_refill_probes"] = len(summary_reads) - before
            for uid in ids:
                unit = loaded.get(uid)
                if unit is None or not visible(unit, query.hierarchy_role):
                    continue
                item = ScoredMemoryUnit(unit, 0.0, RecallChannel.HIERARCHY)
                if add_summary(item, "deferred_vector_keyword_match"):
                    queue.append(item)
                    plan.diagnostics["entry_refill_added"] += 1
                    if uid == reserved_entry:
                        plan.diagnostics["corroborated_entry_added"] = True
                        if plan.diagnostics.get("temporal_anchor_entry_reserved"):
                            plan.diagnostics["temporal_anchor_entry_added"] = True
                        if coarse_keyword_preferred:
                            plan.diagnostics["coarse_keyword_entry_added"] = True
                        if temporal_keyword_preferred:
                            plan.diagnostics["temporal_keyword_entry_added"] = True
                    if uid == exact_alternative:
                        plan.diagnostics["deferred_detail_keyword_added"] = True
            if not queue:
                break
        root = queue.popleft()
        parent, key = root.unit, node_key(root.unit)
        if key in expanded:
            continue
        expanded.add(key)
        groups = defaultdict(list)
        kinds = parent.system_metadata.get("graphmem_child_roles", [])
        if is_recursive(parent) and (
            not isinstance(kinds, list)
            or len(kinds) != len(parent.hierarchy.child_ids)
            or any(
                k not in {HierarchyRole.SNAPSHOT.value, HierarchyRole.TIME_SPAN.value}
                for k in kinds
            )
        ):
            plan.diagnostics["filtered"] += 1
            continue
        for i, uid in enumerate(parent.hierarchy.child_ids):
            role = (
                HierarchyRole(kinds[i])
                if is_recursive(parent) and len(kinds) == len(parent.hierarchy.child_ids)
                else HierarchyRole.SNAPSHOT
            )
            if role is HierarchyRole.SNAPSHOT and not policy.include_original:
                continue
            cs = parent.hierarchy.child_scope_at(i, parent.scope)
            if within_scope(cs, scope):
                groups[(cs.org, cs.space, cs.user, cs.agent, cs.session, role)].append((uid, cs))
        for group in groups.values():
            ids, cs = [uid for uid, _ in group], group[0][1]
            i = parent.hierarchy.child_ids.index(ids[0])
            raw = not is_recursive(parent) or kinds[i] == HierarchyRole.SNAPSHOT.value
            role = HierarchyRole.SNAPSHOT if raw else HierarchyRole.TIME_SPAN
            room = source_limit if raw else summary_limit - len(summaries)
            if room <= 0:
                plan.diagnostics["candidate_limit_reached"] = True
                continue
            if raw:
                source_groups.append((root, cs, ids))
                continue
            width = min(room, policy.branch_width)
            chosen, indexed_scores, query_vectors, hints, detail_hints = indexed_children(
                parent, cs, ids, role, width
            )
            loaded = read(cs, chosen, summary=not raw)
            for uid in chosen:
                child = loaded.get(uid)
                if child is None or not _linked(parent, child) or not visible(child, role):
                    continue
                item = ScoredMemoryUnit(child, root.score, RecallChannel.HIERARCHY)
                if add_summary(item, "query_selected_child_summary"):
                    queue.append(item)
                else:
                    continue
                if uid in hints:
                    plan.reasons[node_key(child)]["parent_event_references"] = hints[uid]
                if uid in detail_hints:
                    plan.reasons[node_key(child)]["parent_detail_references"] = detail_hints[uid]
                plan.links.append(
                    {
                        "source": key,
                        "target": node_key(child),
                        "kind": "contains",
                        "reason": {
                            "verified_child_link": True,
                            **(
                                {"detail_references": detail_hints[uid]}
                                if uid in detail_hints
                                else {}
                            ),
                            "event_references": hints.get(uid, []),
                        },
                    }
                )
    # Allocate slots before raw index queries, not just before materialization.
    # Neither multi-channel overfetch nor adjacency may exceed these quotas.
    batches = (
        [source_groups]
        if refill_source_start is None
        else [source_groups[:refill_source_start], source_groups[refill_source_start:]]
    )
    prioritize_operands = (
        policy.prefer_indexed_operands
        and policy.context_radius
        and program.combine == "union"
        and is_personal_memory_query(query.raw)
    )
    groups, quotas, assigned = deque(), [], 0
    for batch_index, batch in enumerate(batches):
        # Preserve the original graph/tree selections. Deferred entries may use
        # only slots not filled by their indexed hits and adjacent context.
        local_quotas, remaining = [0] * len(batch), source_limit - assigned
        while remaining:
            advanced = False
            for i, (_, _, ids) in enumerate(batch):
                if local_quotas[i] < min(len(ids), policy.source_limit):
                    local_quotas[i] += 1
                    remaining -= 1
                    advanced = True
                    if not remaining:
                        break
            if not advanced:
                break
        before = assigned
        for (root, cs, ids), quota in zip(batch, local_quotas):
            if not quota:
                continue
            primary = max(1, quota // 2) if policy.context_radius else quota
            if prioritize_operands and primary < quota - 1:
                # Use one of the existing adjacent slots for another ranked
                # original. Keep room for nearby context and the same total cap.
                primary += 1
                plan.diagnostics["operand_index_slots"] = (
                    plan.diagnostics.get("operand_index_slots", 0) + 1
                )
            chosen, scores, vectors, hints, detail_hints = indexed_children(
                root.unit, cs, ids, HierarchyRole.SNAPSHOT, primary
            )
            if policy.context_radius:
                for uid in list(chosen):
                    pos = ids.index(uid)
                    for distance in range(1, policy.context_radius + 1):
                        for offset in (-distance, distance):
                            if (
                                0 <= pos + offset < len(ids)
                                and len(chosen) < quota
                                and ids[pos + offset] not in chosen
                            ):
                                chosen.append(ids[pos + offset])
            # Preserve the original index selections and adjacent messages.
            # A summary match may fill only still-empty raw branch slots; it
            # cannot displace existing evidence or redirect summary descent.
            if policy.chunk_source_hints and len(chosen) < quota and not hints and not detail_hints:
                extra, more_scores, more_vectors, references = _spare_chunk_sources(
                    cache, query, root.unit, cs, ids, chosen, quota, needs, policy
                )
                chosen.extend(extra)
                scores.update(more_scores)
                vectors.update(more_vectors)
                detail_hints.update(references)
                plan.diagnostics["source_index_ids_returned"] += len(extra)
                plan.diagnostics["source_vector_matches"] += len(more_vectors)
                plan.diagnostics["detail_reference_index_hits"] += len(extra)
                plan.diagnostics["chunk_reference_index_hits"] += len(extra)
            if chosen:
                selected_ids = deque(chosen[:quota])
                assigned += len(selected_ids)
                groups.append(
                    (root, cs, selected_ids, scores, vectors, hints, detail_hints, batch_index)
                )
        if refill_source_start is not None:
            name = "source_primary_ids" if batch_index == 0 else "source_refill_ids"
            plan.diagnostics[name] = assigned - before
        quotas.extend(local_quotas)
        plan.diagnostics["candidate_limit_reached"] |= bool(batch) and not remaining
    plan.diagnostics["source_branch_quotas"] = quotas
    if (
        policy.relation_source_consensus
        and needs.mode in {"fact", "state"}
        and needs.entities
        and 1 <= len(needs.facets) <= 2
        and not (needs.time_from or needs.time_to)
        and query.span_start is None
        and query.vector
        and not any(signal(item.unit)["clauses"] for item in summaries.values())
    ):
        spare = assigned < source_limit
        evict = None if spare else _swap_unranked_adjacent(groups)
        eligible = [
            (weight, item)
            for key, (weight, item) in probed_endpoints.items()
            if key not in summaries
            and is_recursive(item.unit)
            and signal(item.unit)["entities"]
            and all(
                k == HierarchyRole.SNAPSHOT.value
                for k in item.unit.system_metadata.get("graphmem_child_roles", ())
            )
        ]
        if eligible and (spare or evict is not None):
            _, item = max(eligible, key=lambda pair: pair[0])
            plan.diagnostics["relation_source_index_probe"] = 1
            clue = _indexed_edge_source(cache, query, scope, item.unit, needs)
            if clue is not None:
                cs, uid, score = clue
                existing = any(cs == part[1] and uid in part[2] for part in groups)
                if not existing and add_summary(item, "original_index_under_verified_edge"):
                    if evict is not None:
                        evict[0].remove(evict[1])
                    groups.append((item, cs, deque([uid]), {uid: 1 / 61}, {uid: score}, {}, {}, 0))
                    plan.diagnostics["relation_source_consensus_selected"] = True
    plan.diagnostics["source_posting_ids"] = sum(len(ids) for _, _, ids in source_groups)
    secondary = set()
    while groups and len(originals) < source_limit:
        root, cs, ids, scores, vectors, hints, detail_hints, batch_index = groups.popleft()
        uid = ids.popleft()
        if ids:
            groups.append((root, cs, ids, scores, vectors, hints, detail_hints, batch_index))
        child = read(cs, [uid]).get(uid)
        if (
            child is None
            or not _linked(root.unit, child)
            or not visible(child, HierarchyRole.SNAPSHOT)
        ):
            continue
        key = node_key(child)
        if batch_index and key not in originals:
            secondary.add(key)
        originals.setdefault(key, ScoredMemoryUnit(child, root.score, RecallChannel.HIERARCHY))
        plan.reasons[key] = {
            "selected_by": (
                "event_source_reference_under_selected_summary"
                if uid in hints
                else "detail_source_reference_under_selected_summary"
                if uid in detail_hints
                else "verified_original_under_selected_summary"
            ),
            **signal(child),
            **({"parent_detail_references": detail_hints[uid]} if uid in detail_hints else {}),
            "parent_event_references": hints.get(uid, []),
            "query_similarity": vectors.get(uid, 0),
            "index_score": scores.get(uid, 0),
        }
        plan.diagnostics["event_source_candidates"] += uid in hints
        if policy.detail_source_hints or policy.chunk_source_hints:
            plan.diagnostics["detail_source_candidates"] += uid in detail_hints
        if policy.chunk_source_hints and uid in detail_hints:
            plan.diagnostics["chunk_source_candidates"] += "chunk_seq" in detail_hints[uid][0]
        plan.links.append(
            {
                "source": node_key(root.unit),
                "target": key,
                "kind": "contains",
                "reason": {
                    "verified_child_link": True,
                    **({"detail_references": detail_hints[uid]} if uid in detail_hints else {}),
                    "event_references": hints.get(uid, []),
                },
            }
        )
    plan.diagnostics["candidate_limit_reached"] |= bool(groups)
    qterms = _query_terms(query.raw, policy.normalize_terms)
    if policy.semantic_source_terms and needs.analysis_status in {"used", "used_rules_guard"}:
        for facet in needs.facets:
            qterms |= _query_terms(facet, policy.normalize_terms) | _query_terms(facet, True)

    def ranked_sources(group):
        documents = {
            key: _query_terms(originals[key].unit.content, policy.normalize_terms) for key in group
        }
        frequency = Counter(term for words in documents.values() for term in words)
        weights = {term: math.log(1 + len(documents) / (1 + frequency[term])) for term in qterms}
        vectors = {
            key: plan.reasons[key]["query_similarity"]
            for key in group
            if plan.reasons[key]["query_similarity"] > 0
        }
        ranked = hybrid_order(group, documents, weights, qterms, vectors)
        if policy.source_role_balance:
            balanced = personal_source_order(ranked, originals, query.raw)
            plan.diagnostics["source_role_reordered"] = plan.diagnostics.get(
                "source_role_reordered", 0
            ) + sum(a != b for a, b in zip(ranked, balanced))
            return balanced
        return ranked

    keys = ranked_sources([key for key in originals if key not in secondary])
    if secondary:
        keys.extend(ranked_sources([key for key in originals if key in secondary]))
        plan.refill_candidates = secondary | (summaries.keys() - primary_summaries)
    plan.candidates = [*summaries.values(), *(originals[key] for key in keys)]
    plan.diagnostics.update(
        summary_candidates=len(summaries),
        source_candidates=len(originals),
        sources_added=len(originals),
        source_selection="query_conditioned_graph_index",
        summary_ids_probed=len(summary_reads),
        backend_unit_reads=cache.reads,
        distinct_edges_read=len(cache.edge_ids),
        source_vector_scopes=len(cache.vector_scopes),
        source_scope_limited=(
            cache.limited and len(cache.vector_scopes) >= policy.source_vector_scopes
        ),
        request_read_limited=cache.read_limited or cache.edge_limited,
        query_mode=needs.mode,
        analysis_model_calls=needs.model_calls,
        query_analysis=needs.analysis_status,
    )
    for kind, actual in (("source", len(originals)), ("summary", len(summary_reads))):
        population = limits[f"{kind}_population"]
        plan.diagnostics[f"{kind}_candidate_fraction"] = (
            actual / population if population else 0 if population == 0 else None
        )
    return plan


def pack_relational(
    domain,
    policy,
    scope,
    query,
    candidates,
    packer,
    *,
    max_tokens,
    top_k,
    limits=None,
    entry_pool=None,
    recall_channels=None,
):
    from .graphmem_evidence_graph import pack_graph

    needs = needs_for(query)
    reserve = policy.analysis_token_reserve if needs.model_calls else 0
    if reserve >= max_tokens:
        raise ValidationError("analysis_token_reserve must be smaller than the evidence budget")
    plan = plan_relational(
        domain, policy, scope, query, candidates, limits=limits, entry_pool=entry_pool,
        recall_channels=recall_channels,
    )
    if policy.adaptive_budget:
        from .graphmem_budget import pack_adaptive_graph

        selected, bundle = pack_adaptive_graph(
            plan, packer, policy, query, max_tokens=max_tokens - reserve, top_k=top_k
        )
    else:
        selected, bundle = pack_graph(
            plan,
            packer,
            max_tokens=max_tokens - reserve,
            top_k=top_k,
            summary_tokens=policy.summary_token_budget,
        )
        bundle.navigation.update(
            stages=[
                {
                    "budget": max_tokens - reserve,
                    "tokens": bundle.token_count,
                    "selected": len(selected),
                }
            ],
            stopping_reason="bounded_relation_plan",
        )
    bundle.navigation.update(
        plan.diagnostics,
        analysis_reserved_tokens=reserve,
        requested_token_limit=max_tokens,
        effective_evidence_budget=max_tokens - reserve,
        navigation_strategy="query_relation_program",
    )
    return selected, bundle
