"""Sparse root-to-child relation refinement, using existing features and embeddings."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
from collections import defaultdict, deque
from dataclasses import asdict, dataclass

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.feature_extractor.base import FeatureExtractorProducer
from jiuwen_memory.common.graph_memory import RELATION_KIND, memory_fingerprint
from jiuwen_memory.common.graphmem_semantics import (
    EntityLabel,
    canonical,
    interval_gap,
    iso_time,
    read_semantics,
)
from jiuwen_memory.common.graphmem_structure import node_height, valid_child_role
from jiuwen_memory.common.type_def import HierarchyStatus, LifecycleState, Relation
from jiuwen_memory.construction.associator import AssociationDelta, Associator, AssociatorProducer
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.storage.store_manager import StoreManagerProducer, resolve_name

from .graphmem_features import IndexedGraphVectors, mean_vector

_STOP = frozenset(
    "the a an of to in on at and or is was were are be been with for from that "
    "this his her their them they he she it said told discussed conversation "
    "session summary user assistant about some also has have had".split()
)


@dataclass(frozen=True)
class TopDownRelationPolicy:
    max_units: int = 2000
    max_pairs: int = 10000
    max_edges: int = 8192
    entity_neighbors: int = 4
    time_neighbors: int = 3
    scene_neighbors: int = 4
    root_neighbors: int = 4
    child_neighbors: int = 2
    max_degree: int = 12
    semantic_threshold: float = 0.78
    refinement_threshold: float = 0.60
    temporal_seconds: int = 31 * 86400
    common_entity_sessions: int = 4
    cross_session_only: bool = True
    balance_attributes: bool = False
    isolated_semantic_floor: float = 0.0

    def __post_init__(self):
        if type(self.balance_attributes) is not bool:
            raise ValidationError("balance_attributes must be bool")
        for name, high in (
            ("max_units", 4000),
            ("max_pairs", 100000),
            ("max_edges", 10000),
            ("entity_neighbors", 32),
            ("time_neighbors", 32),
            ("scene_neighbors", 32),
            ("root_neighbors", 32),
            ("child_neighbors", 16),
            ("max_degree", 64),
            ("temporal_seconds", 10 * 365 * 86400),
            ("common_entity_sessions", 100),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= high:
                raise ValidationError(f"invalid top-down policy {name}")
        for value in (self.semantic_threshold, self.refinement_threshold):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValidationError("invalid top-down similarity threshold")
        floor = self.isolated_semantic_floor
        if type(floor) not in (int, float) or not 0 <= floor < self.semantic_threshold:
            raise ValidationError(
                "isolated_semantic_floor must be below the standard similarity threshold"
            )
        if type(self.cross_session_only) is not bool:
            raise ValidationError("cross_session_only must be bool")


@dataclass
class NodeFeatures:
    unit: object
    entities: frozenset[str]
    keywords: frozenset[str]
    vector: list[float]
    events: tuple
    session: str


def _session(unit):
    if unit.system_metadata.get("graphmem_session_scope"):
        return str(unit.system_metadata["graphmem_session_scope"])
    sessions = {scope.session for scope in unit.hierarchy.child_scopes if scope.session}
    if len(sessions) == 1:
        return next(iter(sessions))
    return unit.scope.session or "unknown:" + unit.id


def _cosine(left, right):
    if not left or len(left) != len(right):
        return 0.0
    norm = math.hypot(*left) * math.hypot(*right)
    return sum(a * b for a, b in zip(left, right, strict=True)) / norm if norm else 0.0


def _time_quality(window):
    if window.precision == "year":
        return 0.1
    if window.precision == "month":
        return 0.35
    if window.kind == "uncertain":
        return 1 / (1 + window.width / 86400)
    return 1.0


def _vector_view(value):
    if type(value) is not str or value not in ("scene", "detail"):
        raise ValidationError("graphmem_topdown vector_view must be scene or detail")
    return value


class TopDownGraphMemAssociator(Associator):
    incremental = True

    def __init__(
        self, feature_extractor, policy=None, *, indexed_vectors=None, vector_view="scene"
    ):
        self.extractor = feature_extractor
        self.policy = policy or TopDownRelationPolicy()
        self.indexed_vectors = indexed_vectors
        self.vector_view = _vector_view(vector_view)
        material = asdict(self.policy)
        if not self.policy.isolated_semantic_floor:
            material.pop("isolated_semantic_floor")
        if not self.policy.balance_attributes:
            material.pop("balance_attributes")
        # Preserve the existing scene-policy identity. An explicit projection
        # change invalidates relation caches even if some vectors happen to agree.
        if self.vector_view != "scene":
            material["vector_view"] = self.vector_view
        self.policy_id = (
            "graphmem-topdown-v3:"
            + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        )

    @staticmethod
    def operator_type():
        return OperatorType.ASSOCIATOR

    @staticmethod
    def health():
        return None

    def associate(self, units):
        return self.associate_delta(units, {}).relations

    def associate_delta(self, units, previous):
        p = self.policy
        if len(units) > p.max_units:
            raise ValidationError("top-down candidate window exceeds max_units")
        indexed = {u.id: u for u in units}
        if len(indexed) != len(units) or any(not u.id or u.scope != units[0].scope for u in units):
            raise ValidationError("top-down candidates require unique ids in one exact Scope")
        truth = {u.id: memory_fingerprint(u) for u in units}
        active = {
            u.id: u
            for u in units
            if u.lifecycle is LifecycleState.ACTIVE
            and u.hierarchy.status is HierarchyStatus.ACTIVE
            and node_height(u) > 0
        }
        vector_reader = self.indexed_vectors
        lookup_vectors = (
            vector_reader.read_many(list(active.values()))
            if isinstance(vector_reader, IndexedGraphVectors)
            else None
        )
        features, entity_sessions = {}, defaultdict(set)
        for uid, unit in active.items():
            semantics = read_semantics(unit)
            labels = (
                semantics.entities
                if semantics
                else tuple(EntityLabel(name, "other") for name in unit.entities if name.strip())
            )
            entity_keys = frozenset(key for entity in labels for key in entity.match_keys)
            session = _session(unit)
            for key in entity_keys:
                entity_sessions[key].add(session)
            scene = (
                unit.layers.l1 or "\n".join(s.content for s in unit.segments[1:]) or unit.content
            )
            name_words = {
                word
                for e in labels
                for name in (e.name, *e.aliases)
                for word in re.findall(r"\w+", canonical(name))
            }
            keywords = frozenset(
                canonical(w)
                for w in self.extractor.extract(scene).keywords
                if canonical(w) not in _STOP | name_words and len(w) > 2
            )
            vector = (
                lookup_vectors[uid]
                if lookup_vectors is not None
                else vector_reader(unit)
                if vector_reader
                else []
            )
            if not vector:
                vector = mean_vector([v.vector for v in unit.vectors if v.vector])
            features[uid] = NodeFeatures(
                unit, entity_keys, keywords, vector, semantics.events if semantics else (), session
            )
        children, parent_of = defaultdict(list), {}
        for uid, unit in active.items():
            pid = unit.hierarchy.parent_id
            parent = active.get(pid)
            if (
                parent is not None
                and unit.hierarchy.resolved_parent_scope(unit.scope) == parent.scope
                and valid_child_role(parent, unit)
                and any(
                    cid == uid and parent.hierarchy.child_scope_at(i, parent.scope) == unit.scope
                    for i, cid in enumerate(parent.hierarchy.child_ids)
                )
            ):
                children[pid].append(uid)
                parent_of[uid] = pid
        for group in children.values():
            group.sort()
        roots = sorted(set(active) - parent_of.keys())
        stats = {
            "input_units": len(units),
            "roots": len(roots),
            "pair_checks": 0,
            "vector_scores": 0,
            "refined_pairs": 0,
            "root_pairs": 0,
            "rare_entity_backstops": 0,
            "limited_pairs": 0,
            "model_calls": 0,
            "coarse_routing_pairs": 0,
            "units_with_vectors": sum(bool(f.vector) for f in features.values()),
            "units_with_entities": sum(bool(f.entities) for f in features.values()),
        }
        postings = defaultdict(set)
        starts, ends = [], []
        for uid, f in features.items():
            for entity in f.entities:
                postings[entity].add(uid)
            for event in f.events:
                if event.time.known:
                    starts.append((iso_time(event.time.start).timestamp(), uid))
                    ends.append((iso_time(event.time.end).timestamp(), uid))
        starts.sort()
        ends.sort()
        compared, vectors = {}, {}

        def eligible(a, b):
            if a == b or a not in features or b not in features:
                return False
            if p.cross_session_only and features[a].session == features[b].session:
                return False
            for start, target in ((a, b), (b, a)):
                seen = set()
                while start in parent_of and start not in seen:
                    seen.add(start)
                    start = parent_of[start]
                    if start == target:
                        return False
            return True

        def cosine(a, b):
            pair = tuple(sorted((a, b)))
            if pair not in vectors:
                if len(vectors) >= p.max_pairs:
                    stats["limited_pairs"] += 1
                    return 0.0
                vectors[pair] = _cosine(features[a].vector, features[b].vector)
                stats["vector_scores"] += 1
            return vectors[pair]

        def compare(a, b):
            pair = tuple(sorted((a, b)))
            if pair in compared:
                return compared[pair]
            if not eligible(a, b) or stats["pair_checks"] >= p.max_pairs:
                stats["limited_pairs"] += int(stats["pair_checks"] >= p.max_pairs)
                return None
            stats["pair_checks"] += 1
            left, right = features[a], features[b]
            shared = left.entities & right.entities
            rare = {e for e in shared if len(entity_sessions[e]) < p.common_entity_sessions}
            semantic = max(0.0, cosine(a, b))
            overlap = left.keywords & right.keywords
            lexical = len(overlap) / max(1, len(left.keywords | right.keywords))
            if len(overlap) >= 2 and lexical >= 0.3:
                semantic = max(semantic, min(0.9, 0.6 + lexical * 0.3))
            matched, temporal = [], 0.0
            for ea in left.events:
                for eb in right.events:
                    gap = interval_gap(ea.time, eb.time)
                    context = bool(set(ea.entity_keys) & set(eb.entity_keys))
                    if gap <= p.temporal_seconds and (context or semantic >= p.semantic_threshold):
                        quality = min(_time_quality(ea.time), _time_quality(eb.time))
                        strength = quality / (1 + gap / 86400)
                        if strength > temporal:
                            temporal = strength
                            matched = [
                                {
                                    "left": ea.key,
                                    "right": eb.key,
                                    "gap_seconds": gap,
                                    "quality": quality,
                                }
                            ]
            attributes = []
            if rare:
                attributes.append("entity")
            if semantic >= p.semantic_threshold:
                attributes.append("semantic")
            if temporal >= 0.25 or (temporal > 0 and attributes):
                attributes.append("temporal")
            if shared and attributes and "entity" not in attributes:
                attributes.append("entity")
            score = min(
                1.0,
                max(0.8 if rare else 0.0, semantic, temporal * 0.85)
                + max(0, len(attributes) - 1) * 0.025,
            )
            value = (score, sorted(attributes), sorted(shared), matched) if attributes else None
            compared[pair] = value
            return value

        def candidates(uid, allowed, *, backstop=False):
            allowed = {x for x in allowed if eligible(uid, x)}
            found = set()
            for key in sorted(features[uid].entities, key=lambda e: (len(postings[e]), e)):
                if len(entity_sessions[key]) >= p.common_entity_sessions:
                    continue
                matches = postings[key] & allowed
                found.update(
                    sorted(matches, key=lambda x: (-cosine(uid, x), x))[: p.entity_neighbors]
                )
            if backstop:
                return found
            times = set()
            for event in features[uid].events:
                if not event.time.known:
                    continue
                for value in (
                    iso_time(event.time.start).timestamp(),
                    iso_time(event.time.end).timestamp(),
                ):
                    for ordered in (starts, ends):
                        # Same-session ancestors must not crowd eligible neighbors
                        # out of the time window before nearest-k selection.
                        ordered = [entry for entry in ordered if entry[1] in allowed]
                        pos = bisect.bisect_left(ordered, (value, ""))
                        radius = p.time_neighbors * 4
                        times.update(
                            other
                            for _, other in ordered[max(0, pos - radius) : pos + radius]
                            if other in allowed
                        )

            def time_distance(other):
                return min(
                    (
                        interval_gap(a.time, b.time)
                        for a in features[uid].events
                        for b in features[other].events
                    ),
                    default=float("inf"),
                )

            found.update(sorted(times, key=lambda x: (time_distance(x), x))[: p.time_neighbors])
            found.update(sorted(allowed, key=lambda x: (-cosine(uid, x), x))[: p.scene_neighbors])
            return found

        proposals, frontier, routed = {}, deque(), set()

        def routing_score(a, b, match):
            if match is not None:
                return match[0]
            # A broad parent can dilute a specific shared child topic. It may
            # warrant comparing children without asserting a parent relation.
            if (
                (children.get(a) or children.get(b))
                and eligible(a, b)
                and cosine(a, b) >= min(p.refinement_threshold, p.semantic_threshold)
            ):
                return max(0.0, cosine(a, b))
            return None

        def ranked_routes(uid, others, limit):
            options = []
            for other in others:
                if not eligible(uid, other):
                    continue
                match = compare(uid, other)
                score = routing_score(uid, other, match)
                if score is not None:
                    options.append((match is None, score, other))
            # A tentative broad match must not evict an already supported entity,
            # time or scene relation just because its cosine is larger.
            ordered = sorted(options, key=lambda x: (x[0], -x[1], x[2]))
            if not p.balance_attributes:
                return [other for _, _, other in ordered[:limit]]
            chosen, covered = [], set()
            while ordered and len(chosen) < limit:
                best = max(
                    range(len(ordered)),
                    key=lambda i: (
                        len(set((compare(uid, ordered[i][2]) or (0, []))[1]) - covered),
                        -i,
                    ),
                )
                _, _, other = ordered.pop(best)
                chosen.append(other)
                match = compare(uid, other)
                if match:
                    covered.update(match[1])
            return chosen

        def propose(a, b, stage):
            pair = tuple(sorted((a, b)))
            match = compare(*pair)
            if routing_score(*pair, match) is None:
                return
            if match is not None and pair not in proposals:
                proposals[pair] = (match, stage)
                stats[stage] += 1
            if pair not in routed:
                if len(routed) >= p.max_pairs:
                    stats["limited_pairs"] += 1
                    return
                routed.add(pair)
                frontier.append(pair)
                stats["coarse_routing_pairs"] += int(match is None)

        for root in roots:
            for other in ranked_routes(root, candidates(root, roots), p.root_neighbors):
                propose(root, other, "root_pairs")
        descended = set()
        while frontier:
            left, right = frontier.popleft()
            pair = (left, right)
            if pair in descended:
                continue
            descended.add(pair)
            a_children = children.get(left) or [left]
            b_children = children.get(right) or [right]
            if a_children == [left] and b_children == [right]:
                continue
            for first, others in [(a, b_children) for a in a_children] + [
                (b, a_children) for b in b_children
            ]:
                for other in ranked_routes(first, others, p.child_neighbors):
                    propose(first, other, "refined_pairs")
        # A rare exact entity in a lower summary can recover an omitted parent
        # topic. Common speakers cannot open an all-pairs escape hatch.
        for uid in sorted(features):
            if uid not in parent_of:
                continue
            for other in candidates(uid, features.keys(), backstop=True):
                propose(uid, other, "rare_entity_backstops")
        degree, selected = defaultdict(int), []
        ordered_proposals = sorted(proposals.items(), key=lambda x: (-x[1][0][0], x[0]))
        if p.balance_attributes:
            reserved, seen_attributes = set(), defaultdict(set)
            for pair, (match, _) in ordered_proposals:
                attributes = set(match[1])
                if any(attributes - seen_attributes[uid] for uid in pair):
                    reserved.add(pair)
                    for uid in pair:
                        seen_attributes[uid].update(attributes)
            ordered_proposals.sort(key=lambda x: (x[0] not in reserved, -x[1][0][0], x[0]))
        for (a, b), (match, stage) in ordered_proposals:
            if len(selected) >= p.max_edges:
                break
            if degree[a] >= p.max_degree or degree[b] >= p.max_degree:
                continue
            degree[a] += 1
            degree[b] += 1
            score, attributes, shared, matched = match
            selected.append(
                Relation(
                    a,
                    b,
                    RELATION_KIND,
                    score=score,
                    metadata={
                        "attributes": attributes,
                        "shared_entities": shared,
                        "matched_events": matched,
                        "score": score,
                        "stage": stage,
                        "policy_id": self.policy_id,
                        "fingerprints": {a: truth[a], b: truth[b]},
                    },
                )
            )
        stats["ordinary_relation_edges"] = len(selected)
        stats["relation_summary_nodes"] = len(features)
        if p.isolated_semantic_floor and 2 * len(selected) < len(features):
            stats["isolated_semantic_backstop_edges"] = 0
            remaining = min(p.root_neighbors, p.max_edges - len(selected))
            for (a, b), score in sorted(vectors.items(), key=lambda item: (-item[1], item[0])):
                if not remaining or score < p.isolated_semantic_floor:
                    break
                if (
                    degree[a]
                    and degree[b]
                    or degree[a] >= p.max_degree
                    or degree[b] >= p.max_degree
                    or not eligible(a, b)
                ):
                    continue
                selected.append(
                    Relation(
                        a,
                        b,
                        RELATION_KIND,
                        score=score,
                        metadata={
                            "attributes": ["semantic"],
                            "shared_entities": [],
                            "matched_events": [],
                            "score": score,
                            "stage": "isolated_semantic_backstop",
                            "policy_id": self.policy_id,
                            "fingerprints": {a: truth[a], b: truth[b]},
                        },
                    )
                )
                degree[a] += 1
                degree[b] += 1
                remaining -= 1
                stats["isolated_semantic_backstop_edges"] += 1
        adjacency = defaultdict(list)
        for rel in selected:
            stamp = [
                rel.source_id,
                rel.target_id,
                rel.score,
                rel.metadata["attributes"],
                rel.metadata["matched_events"],
                truth[rel.source_id],
                truth[rel.target_id],
            ]
            adjacency[rel.source_id].append(stamp)
            adjacency[rel.target_id].append(stamp)
        cache = {}
        for uid in indexed:
            material = [truth[uid], features[uid].vector if uid in features else [], adjacency[uid]]
            cache[uid] = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        affected = sorted(
            uid for uid in indexed if previous.get(uid) != self.policy_id + ":" + cache[uid]
        )
        stats.update(
            changed_units=len(affected),
            candidate_pairs=len(proposals),
            routing_pairs=len(routed),
            selected_edges=len(selected),
            max_degree=max(degree.values(), default=0),
        )
        affected_set = set(affected)
        return AssociationDelta(
            relations=[
                r for r in selected if r.source_id in affected_set or r.target_id in affected_set
            ],
            affected_ids=affected,
            fingerprints=truth,
            policy_id=self.policy_id,
            stats=stats,
            input_fingerprints=cache,
        )


@AssociatorProducer.register("graphmem_topdown")
def _build(config):
    options = config.get("policy", {})
    if not isinstance(options, dict) or set(options) - set(
        TopDownRelationPolicy.__dataclass_fields__
    ):
        raise ValidationError("invalid graphmem_topdown policy")
    reader = None
    vector_view = _vector_view(config.get("vector_view", "scene"))
    use_vectors = config.get("use_indexed_vectors", True)
    if type(use_vectors) is not bool:
        raise ValidationError("use_indexed_vectors must be bool")
    if use_vectors:
        manager = StoreManagerProducer.resolve(config)
        vector_name = resolve_name(config, "vector_store")
        kv_name = resolve_name(config, "kv_store")
        reader = IndexedGraphVectors(
            manager.vector(vector_name) if manager.has_vector(vector_name) else None,
            manager.vector("layers_l1") if manager.has_vector("layers_l1") else None,
            manager.kv(kv_name) if manager.has_kv(kv_name) else None,
            prefer_l1=vector_view == "scene",
        )
    return TopDownGraphMemAssociator(
        FeatureExtractorProducer.dep(config, default="keyword"),
        TopDownRelationPolicy(**options),
        indexed_vectors=reader,
        vector_view=vector_view,
    )
