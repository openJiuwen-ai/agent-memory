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
    RelationStatus,
    RoleViewQuery,
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

    def build(
        self,
        scope: Scope,
        request: RoleViewQuery,
        *,
        graph: GraphStore,
        read: Callable[[MemoryRef], MemoryUnit],
        discover: Callable[[str, int], list[MemoryUnit]],
        resolve_edge: Callable[[str], tuple],
        background: list[dict] | None = None,
        registry_revision: int | None = None,
    ) -> dict:
        policy = self.catalog.policy(request.duty)
        budget = self.catalog.effective_budget(request.duty, request.budget)
        deadline = monotonic() + budget.timeout_ms / 1000
        result = dict(
            schema_version="role-view/1",
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
                duty=request.duty,
                policy_version=self.catalog.version,
                model_version=MODEL_VERSION,
                effective_budget=asdict(budget),
                relation_coverage="unknown",
                completeness="partial",
                continuation_ready=False,
                unchecked_scope=["additional_history", "relation_coverage", "environment"],
            ),
        )
        nodes: dict[str, tuple[MemoryRef, MemoryUnit, dict]] = {}
        queue: deque = deque()
        seen_edges: set[str] = set()
        processed: set[str] = set()
        disputed: set[str] = set()

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
                kind={
                    "judgment": "inference",
                    "result": "observation",
                    "unclassified": "statement",
                    "correction": "statement",
                }.get(kind, kind),
                status="reported",
                classification_confidence=unit.system_metadata.get(
                    "continuity_classification_confidence", "unknown"
                ),
                evidence_refs=[asdict(ref)],
                conditions=[],
                duty_ids=list(unit.system_metadata.get("continuity_duties", [])),
                discovery=discovery,
                path_ids=[],
            )
            # A recorded execution report is not independently verified by the memory system.
            if group == "experiences":
                item["status"] = "unverified"
            nodes[ref.key] = ref, unit, item
            result[group].append(item)
            return True

        def seed(ref: MemoryRef, discovery: str, required: bool = False) -> None:
            if add(ref, discovery, required):
                if asdict(ref) not in result["anchors"]:
                    result["anchors"].append(asdict(ref))
                    queue.append((ref, ref, [], 0))

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
                    # With fixed anchors, only independent procedural experiences supplement them.
                    if (
                        (request.anchor or background)
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
            current, anchor, steps, depth = queue.popleft()
            if current.key in processed:
                continue
            processed.add(current.key)
            if monotonic() >= deadline:
                gap("budget_exhausted", True)
                break
            try:
                edges = graph.incident_edges(
                    scope, current.key, limit=budget.max_edges - len(seen_edges) + 1
                )
            except Exception:
                gap("graph_unavailable", True)
                break
            if len(edges) > budget.max_edges - len(seen_edges):
                gap("unchecked_dependencies", True)
            edges.sort(
                key=lambda e: min(
                    (r.priority for r in policy if r.edge_type == e.relation), default=100
                )
            )
            for edge in edges:
                if depth >= budget.max_depth:
                    gap("unchecked_dependencies", True)
                    continue
                if edge.id in seen_edges:
                    continue
                if len(seen_edges) >= budget.max_edges or monotonic() >= deadline:
                    gap("budget_exhausted", True)
                    break
                seen_edges.add(edge.id)
                try:
                    relation, relation_ref = resolve_edge(edge.id)
                    if relation.status == RelationStatus.RETRACTED:
                        continue
                    # Revalidate the actual edge semantics after reading the authoritative head.
                    actual_direction = "out" if relation.source.key == current.key else "in"
                    if current.key not in {relation.source.key, relation.target.key}:
                        gap("unchecked_dependencies", True)
                        continue
                    if not any(
                        r.matches(relation.relation_type, actual_direction, depth) for r in policy
                    ):
                        continue
                    for ref in [relation.source, relation.target, *relation.evidence_refs]:
                        read(ref)
                except (AgentMemoryError, ValueError, KeyError):
                    gap("reference_unavailable", True)
                    continue  # Never traverse through a hidden/stale endpoint.
                if relation.status != RelationStatus.CONFIRMED:
                    if relation.relation_type == "contradicts":
                        disputed.update((relation.source.key, relation.target.key))
                        gap("unresolved_conflict", True)
                    gap("review_pending", True)
                    continue
                target = relation.target if actual_direction == "out" else relation.source
                if not add(target, "relation", True):
                    continue
                step = dict(
                    relation_ref=asdict(relation_ref),
                    relation_id=relation.relation_id,
                    revision=relation.revision,
                    relation_type=relation.relation_type,
                    direction=actual_direction,
                    source=asdict(relation.source),
                    target=asdict(relation.target),
                    status=relation.status.value,
                    evidence_refs=[asdict(r) for r in relation.evidence_refs],
                    conditions=relation.conditions,
                )
                path_id = f"p{len(result['paths']) + 1}"
                result["paths"].append(
                    dict(path_id=path_id, anchor=asdict(anchor), steps=[*steps, step])
                )
                nodes[target.key][2]["path_ids"].append(path_id)
                for evidence in relation.evidence_refs:
                    if evidence.key not in nodes:
                        if add(evidence, "relation", True):
                            nodes[evidence.key][2]["path_ids"].append(path_id)
                # Support follows the relation's semantic target, even on a backward walk.
                # Confirmation of a method, cause, requirement or test result alone is not
                # positive support. Keep one exact condition set; paths retain alternatives.
                supported = nodes[relation.target.key][2]
                if (
                    relation.relation_type == "supports"
                    and relation.evidence_refs
                    and relation.conditions
                    and supported["kind"] == "inference"
                    and supported["status"] == "reported"
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
                    )
                    result["risks"].append(risk)
                queue.append((target, anchor, [*steps, step], depth + 1))
        # Conflict status wins regardless of traversal order; a positive path cannot
        # erase an unresolved counterexample or an explicit refutation.
        for key in disputed:
            if key in nodes and nodes[key][2]["status"] == "supported_in_conditions":
                nodes[key][2]["status"] = "needs_review"
        # Exhausting the graph queue does not establish ingestion or environment coverage.
        gap("relation_coverage_unknown")
        gap("environment_unconfirmed", True)
        return result


@RoleViewProducer.register("bounded")
def build(config):
    return RoleViewBuilder()
