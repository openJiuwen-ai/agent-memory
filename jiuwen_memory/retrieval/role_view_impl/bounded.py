"""Bounded role-conditioned relation expansion over authorized memory callbacks."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from time import monotonic

from jiuwen_memory.common.errors import (
    AgentMemoryError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from jiuwen_memory.common.relations.catalog import RelationCatalog
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.common.type_def.continuity import (
    MODEL_VERSION,
    MemoryRef,
    RelationRecord,
    RelationStatus,
    RoleViewQuery,
    view_kind,
    view_status,
)
from jiuwen_memory.retrieval.role_view import RoleView, RoleViewProducer
from jiuwen_memory.storage.graph import GraphStore


class RoleViewBuilder(RoleView):
    """No identities or backend types: authorization is retained by the API read port."""

    def __init__(self, catalog: RelationCatalog | None = None):
        self.catalog = catalog or RelationCatalog()

    def health(self):
        self.catalog.policy("general")

    def configure_relations(self, catalog):
        self.catalog = catalog

    @staticmethod
    def _duties(request: RoleViewQuery) -> list[str]:
        return [request.duty] if isinstance(request.duty, str) else list(request.duty)

    def _allows(self, duty: str, relation: str, direction: str, hop: int) -> bool:
        return any(r.matches(relation, direction, hop) for r in self.catalog.policy(duty))

    def build(
        self,
        scope: Scope,
        request: RoleViewQuery,
        *,
        graph: GraphStore,
        read: Callable[[MemoryRef], MemoryUnit],
        discover: Callable[[str, int], list[MemoryUnit]],
        resolve_edge: Callable[[str], RelationRecord],
        background: list[dict] | None = None,
        registry_revision: int | None = None,
    ) -> dict:
        duties = self._duties(request)
        budget = self.catalog.effective_budget(duties, request.budget)
        deadline = monotonic() + budget.timeout_ms / 1000
        duty_coverage = {
            d: {"status": "not_started", "expanded_edges": 0, "reason": ""} for d in duties
        }
        result = dict(
            schema_version="role-view/2",
            task_id=request.task_id or None,
            anchors=[],
            paths=[],
            facts=[],
            judgments=[],
            experiences=[],
            open_issues=[],
            risks=[],
            gaps=[],
            meta=dict(
                based_on_handoff=None,
                generated_at=datetime.now(UTC).isoformat(),
                registry_revision=registry_revision,
                duty=duties[0],
                duties=list(duties),
                policy_version=self.catalog.version,
                model_version=MODEL_VERSION,
                effective_budget=asdict(budget),
                relation_coverage="unknown",
                completeness="partial",
                continuation_ready=False,
                unchecked_scope=["additional_history", "relation_coverage", "environment"],
                duty_coverage=duty_coverage,
            ),
        )
        nodes: dict[str, tuple[MemoryRef, MemoryUnit, dict]] = {}
        expanded: dict[str, int] = {d: 0 for d in duties}
        disputed: set[str] = set()
        # Path-state bookkeeping. A path state is (node, valid duties, hops taken);
        # valid is the intersection of matched_duties so far (empty = cross-duty).
        valid_states: dict[str, list[tuple[frozenset[str], int]]] = {}
        cross_states: dict[str, int] = {}
        resolved: dict[str, tuple | None] = {}
        expanded_edges_count = 0
        truncate_reason: str | None = None

        def mark_truncated(reason: str) -> None:
            nonlocal truncate_reason
            if truncate_reason is None:
                truncate_reason = reason

        def gap(code: str, blocking: bool = False) -> None:
            if not any(g["code"] == code and g["blocking"] == blocking for g in result["gaps"]):
                result["gaps"].append(
                    dict(
                        code=code,
                        message=code.replace("_", " "),
                        blocking=blocking,
                        affected_item_ids=[],
                    )
                )

        def add(ref: MemoryRef, discovery: str, required: bool = False) -> bool:
            if ref.key in nodes:
                if nodes[ref.key][0] != ref:
                    gap("reference_unavailable", required)
                    return False
                return True
            if len(nodes) >= budget.max_nodes or monotonic() >= deadline:
                gap("budget_exhausted", required)
                mark_truncated("budget_exhausted")
                return False
            try:
                unit = read(ref)
            except (NotFoundError, PermissionDeniedError, ConflictError):
                gap("reference_unavailable", required)
                return False
            except AgentMemoryError:
                gap("retrieval_partial", required)
                return False
            kind = str(unit.system_metadata.get("continuity_kind", "statement"))
            group = {
                "judgment": "judgments",
                "experience": "experiences",
                "correction": "judgments",
            }.get(kind, "facts")
            item = dict(
                id=f"m{len(nodes) + 1}",
                text=unit.content,
                kind=view_kind(kind),
                status=view_status(kind),
                classification_confidence=unit.system_metadata.get(
                    "continuity_classification_confidence", "unknown"
                ),
                evidence_refs=[asdict(ref)],
                conditions=[],
                duty_ids=list(unit.system_metadata.get("continuity_duties", [])),
                discovery=discovery,
                path_ids=[],
                valid_duties=[],
                common=discovery in {"background", "anchor", "semantic_candidate"},
                required=False,
            )
            # A recorded experience is a reusable-strategy candidate until it gains a
            # confirmed, evidenced, conditioned support (or loses one to an explicit
            # negation); it is never auto-confirmed by the memory system.
            if group == "experiences":
                item["status"] = "candidate"
            nodes[ref.key] = ref, unit, item
            result[group].append(item)
            return True

        def enqueue(nkey: str, nvalid: frozenset[str], nhops: int, anchor, steps) -> None:
            if len(result["paths"]) >= budget.max_paths:
                mark_truncated("budget_exhausted")
                gap("budget_exhausted", True)
                return
            if nvalid:
                kept = valid_states.setdefault(nkey, [])
                # Dominance: S (valid, hops) dominates T iff S.valid ⊇ T.valid and
                # hops_s <= hops_t (remaining depth is monotonic in hops).
                for v, h in list(kept):
                    if nvalid.issuperset(v) and nhops <= h:
                        kept.remove((v, h))
                if any(nvalid.issubset(v) and nhops >= h for v, h in kept):
                    return
                if len(kept) >= len(duties) + 1:
                    mark_truncated("unchecked_dependencies")
                    gap("unchecked_dependencies", True)
                    return
                kept.append((nvalid, nhops))
            else:
                current = cross_states.get(nkey)
                if current is not None and current <= nhops:
                    return
                cross_states[nkey] = nhops
            queue.append((nkey, nvalid, nhops, anchor, steps))

        def edge_priority(edge) -> int:
            priorities = []
            for duty in duties:
                for rule in self.catalog.policy(duty):
                    if rule.edge_type == edge.relation:
                        priorities.append(rule.priority)
            return min(priorities, default=100)

        def resolve(edge_id: str):
            if edge_id in resolved:
                return resolved[edge_id]
            try:
                relation = resolve_edge(edge_id)
                for ref in [relation.source, relation.target, *relation.evidence_refs]:
                    read(ref)
                resolved[edge_id] = relation
            except (AgentMemoryError, ValueError, KeyError):
                resolved[edge_id] = None
            return resolved[edge_id]

        queue: deque = deque()

        def seed(ref: MemoryRef, discovery: str, required: bool = False) -> None:
            if add(ref, discovery, required):
                # add 的 required 只决定读取失败时缺口是否阻塞（关系端点也传 True）；
                # 条目标注的「必需依据」只来自必需背景与显式 anchor，且只升不降。
                if required:
                    nodes[ref.key][2]["required"] = True
                if asdict(ref) not in result["anchors"]:
                    result["anchors"].append(asdict(ref))
                    enqueue(ref.key, frozenset(duties), 0, ref, [])

        for entry in background or []:
            seed(MemoryRef.parse(entry["ref"]), "background", entry["required"])
        if request.anchor:
            seed(request.anchor, "anchor", True)
        query = request.query.strip()
        if not query and nodes:
            query = next(iter(nodes.values()))[1].content
        if query and len(nodes) < budget.max_nodes and monotonic() < deadline:
            try:
                for unit in discover(query, min(5, budget.max_nodes - len(nodes))):
                    has_background = bool(request.anchor or background)
                    if (
                        has_background
                        and unit.tier.value != "procedural"
                        and unit.system_metadata.get("continuity_kind") != "experience"
                    ):
                        continue
                    seed(MemoryRef.of(unit), "semantic_candidate")
            except AgentMemoryError:
                gap("retrieval_partial")
        if not nodes:
            gap("missing_input", True)

        while queue:
            node_key, valid, hops, anchor, steps = queue.popleft()
            if valid:
                if (valid, hops) not in valid_states.get(node_key, []):
                    continue
            elif cross_states.get(node_key) != hops:
                continue
            if monotonic() >= deadline:
                mark_truncated("timeout")
                gap("budget_exhausted", True)
                break
            try:
                edges = graph.incident_edges(scope, node_key, limit=budget.max_edges + 1)
            except Exception:
                mark_truncated("graph_unavailable")
                gap("graph_unavailable", True)
                break
            if len(edges) > budget.max_edges:
                gap("unchecked_dependencies", True)
            edges.sort(key=edge_priority)
            for edge in edges:
                if expanded_edges_count >= budget.max_edges or monotonic() >= deadline:
                    reason = "timeout" if monotonic() >= deadline else "budget_exhausted"
                    mark_truncated(reason)
                    gap("budget_exhausted", True)
                    break
                if len(result["paths"]) >= budget.max_paths:
                    mark_truncated("budget_exhausted")
                    gap("budget_exhausted", True)
                    break
                if hops >= budget.max_depth:
                    mark_truncated("depth_limit")
                    gap("unchecked_dependencies", True)
                    continue
                pair = resolve(edge.id)
                if pair is None:
                    gap("reference_unavailable", True)
                    continue
                relation = pair
                if relation.status == RelationStatus.RETRACTED:
                    continue
                actual_direction = "out" if relation.source.key == node_key else "in"
                if node_key not in {relation.source.key, relation.target.key}:
                    gap("unchecked_dependencies", True)
                    continue
                matched = frozenset(
                    d
                    for d in duties
                    if self._allows(d, relation.relation_type, actual_direction, hops)
                )
                if not matched:
                    continue  # No duty allows this edge at this hop.
                if relation.status != RelationStatus.CONFIRMED:
                    if relation.relation_type == "contradicts":
                        disputed.update((relation.source.key, relation.target.key))
                        gap("unresolved_conflict", True)
                    gap("review_pending", True)
                    continue
                target = relation.target if actual_direction == "out" else relation.source
                new_valid = valid & matched
                if not add(target, "relation", True):
                    continue
                step = dict(
                    relation_id=relation.relation_id,
                    revision=relation.revision,
                    relation_type=relation.relation_type,
                    direction=actual_direction,
                    source=asdict(relation.source),
                    target=asdict(relation.target),
                    status=relation.status.value,
                    evidence_refs=[asdict(r) for r in relation.evidence_refs],
                    conditions=relation.conditions,
                    matched_duties=sorted(matched),
                )
                path_id = f"p{len(result['paths']) + 1}"
                result["paths"].append(
                    dict(
                        path_id=path_id,
                        anchor=asdict(anchor),
                        steps=[*steps, step],
                        valid_duties=sorted(new_valid),
                    )
                )
                item = nodes[target.key][2]
                if path_id not in item["path_ids"]:
                    item["path_ids"].append(path_id)
                for d in new_valid:
                    if d not in item["valid_duties"]:
                        item["valid_duties"].append(d)
                for evidence in relation.evidence_refs:
                    if evidence.key not in nodes:
                        if add(evidence, "relation", True):
                            nodes[evidence.key][2]["path_ids"].append(path_id)
                    if evidence.key in nodes:
                        for d in new_valid:
                            if d not in nodes[evidence.key][2]["valid_duties"]:
                                nodes[evidence.key][2]["valid_duties"].append(d)
                # Support follows the relation's semantic target, even on a backward walk.
                # Confirmation of a method, cause, requirement or test result alone is not
                # positive support. A supported experience keeps its exact conditions.
                supported = nodes[relation.target.key][2]
                evidenced_support = (
                    relation.relation_type == "supports"
                    and relation.evidence_refs
                    and relation.conditions
                )
                if (
                    evidenced_support
                    and supported["kind"] in {"inference", "experience"}
                    and supported["status"] in {"reported", "candidate"}
                ):
                    supported["status"] = "supported_in_conditions"
                    supported["conditions"] = list(relation.conditions)
                    for ref in relation.evidence_refs:
                        if asdict(ref) not in supported["evidence_refs"]:
                            supported["evidence_refs"].append(asdict(ref))
                    if path_id not in supported["path_ids"]:
                        supported["path_ids"].append(path_id)
                if relation.relation_type == "contradicts":
                    decision = relation.resolution
                    if decision == "escalate" or not decision:
                        disputed.update((relation.source.key, relation.target.key))
                        gap("conflict_escalated" if decision else "unresolved_conflict", True)
                    elif decision in {"prefer_a", "prefer_b"}:
                        loser = relation.target if decision == "prefer_a" else relation.source
                        if loser.key in nodes:
                            nodes[loser.key][2]["status"] = "refuted"
                    risk = dict(nodes[target.key][2])
                    risk.update(
                        id=f"risk{len(result['risks']) + 1}",
                        kind="risk",
                        text=relation.resolution_reason or "Unresolved contradiction",
                        status="needs_review"
                        if not decision or decision == "escalate"
                        else "reported",
                        conditions=relation.conditions,
                        path_ids=[path_id],
                        common=True,
                    )
                    result["risks"].append(risk)
                # Shared-edge accounting: the truth recheck happens once, but the edge
                # is credited to every duty that allowed it.
                expanded_edges_count += 1
                for d in matched:
                    expanded[d] += 1
                enqueue(target.key, new_valid, hops + 1, anchor, [*steps, step])

        for key in disputed:
            if key in nodes and nodes[key][2]["status"] == "supported_in_conditions":
                nodes[key][2]["status"] = "needs_review"
        # Reuse only the already-authorized, budget-bounded relation snapshot. A
        # disputed premise invalidates its dependents even if a supports edge was
        # visited earlier. Direction is semantic: A requires B makes A depend on B.
        affected = disputed | {
            key for key, (_, _, item) in nodes.items()
            if item["status"] in {"refuted", "needs_review"}
        }
        dependencies: dict[str, set[str]] = {}
        for pair in resolved.values():
            if pair is None:
                continue
            relation = pair
            if relation.status != RelationStatus.CONFIRMED:
                continue
            if relation.relation_type == "supports":
                premise, conclusion = relation.source.key, relation.target.key
            elif relation.relation_type == "requires":
                premise, conclusion = relation.target.key, relation.source.key
            else:
                continue
            if premise in nodes and conclusion in nodes:
                dependencies.setdefault(premise, set()).add(conclusion)
        pending = deque(affected & nodes.keys())
        while pending and monotonic() < deadline:
            premise = pending.popleft()
            for conclusion in dependencies.get(premise, ()):
                if conclusion not in affected:
                    affected.add(conclusion)
                    pending.append(conclusion)
        for key in affected:
            if key in nodes:
                item = nodes[key][2]
                if item["status"] != "refuted":
                    item["status"] = "needs_review"
        if affected:
            # A duty-limited view cannot prove the entire dependency graph was read.
            gap("unchecked_dependencies", True)
        for d in duties:
            cov = duty_coverage[d]
            cov["expanded_edges"] = expanded[d]
            if expanded[d] == 0 and truncate_reason == "budget_exhausted":
                cov["status"] = "not_started"
                cov["reason"] = "budget_exhausted"
            elif truncate_reason:
                cov["status"] = "truncated"
                cov["reason"] = truncate_reason
            else:
                cov["status"] = "complete"
        gap("relation_coverage_unknown")
        gap("environment_unconfirmed", True)
        return result


@RoleViewProducer.register("bounded")
def build(config):
    return RoleViewBuilder()
