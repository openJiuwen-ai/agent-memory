"""Task/event/relation orchestration; caller supplies authorized memory operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Any

from jiuwen_memory.common.errors import (
    AgentMemoryError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.relations.catalog import RelationCatalog
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    MemoryRef,
    RelationRecord,
    RelationStatus,
    TaskRequest,
    canonical,
    fingerprint,
)
from jiuwen_memory.construction.associator import AssociatorProducer
from jiuwen_memory.construction.conflict_resolver import ConflictResolverProducer
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.storage.graph import GraphStore
from jiuwen_memory.storage.store_manager import StoreManagerProducer
from jiuwen_memory.storage.types import IndexWriteMode

from .base import Continuity, ContinuityProducer
from .repository import ContinuityRepository

logger = get_logger(__name__)

ReadMemory = Callable[[MemoryRef], MemoryUnit]
WriteMemory = Callable[[str, dict[str, Any]], list[MemoryUnit]]


class ContinuityService(Continuity):
    def __init__(
        self,
        repository: ContinuityRepository,
        graph: GraphStore,
        view_builder,
        *,
        projector,
        catalog=None,
        conflict_resolver=None,
        associator=None,
    ):
        if not graph.supports_incident_edges:
            raise ValidationError("continuity graph must support directed incident edges")
        self.catalog = catalog or RelationCatalog()
        self.projector = projector
        self.conflict_resolver = conflict_resolver
        self.associator = associator
        if associator is not None:
            associator.configure_relations(self.catalog)
        view_builder.configure_relations(self.catalog)
        self.view_builder = view_builder
        self.repository = repository
        self.graph = graph

    def health(self) -> None:
        self.repository.kv.health()
        self.graph.health()
        self.projector.health()
        self.view_builder.health()
        if self.conflict_resolver:
            self.conflict_resolver.health()
        if self.associator:
            self.associator.health()

    def task(self, scope: Scope, request: TaskRequest, owner: str, read: ReadMemory) -> dict:
        repo = self.repository
        if request.operation == "find":
            return {
                "tasks": [
                    {"task_id": t["task_id"], "revision": t["revision"]}
                    for t in repo.list(scope, "tasks")
                    if any(
                        b["active"]
                        and b["connector_id"] == request.connector_id
                        and b["context_id"] == request.context_id
                        for b in t["bindings"]
                    )
                ]
            }
        if request.operation == "get":
            task = repo.get(scope, "tasks", request.task_id)
            return self.task_view(task, read)
        task_id = request.task_id
        initial = None
        if request.operation == "create":
            task_id = fingerprint({"owner": owner, "request_id": request.request_id})
            initial = dict(
                task_id=task_id, owner=owner, revision=0, state="open", bindings=[], background=[]
            )
        for ref in request.background:
            read(ref)

        def change(task: dict) -> None:
            if task["state"] != "open":
                raise ConflictError("task", message="task is closed")
            if request.operation == "bind":
                if not request.connector_id or not request.context_id:
                    raise ValidationError("connector and context required")
                task["bindings"].append(
                    dict(
                        binding_id=fingerprint({"task": task_id, "request": request.request_id}),
                        connector_id=request.connector_id,
                        context_id=request.context_id,
                        context_source=request.context_source,
                        created_by=owner,
                        active=True,
                    )
                )
            elif request.operation == "unbind":
                binding = self.binding(task, request.binding_id)
                binding["active"] = False
                binding["revoked_by"] = owner
                binding["revocation_reason"] = request.reason
            elif request.operation == "close":
                task["state"] = "closed"
            for ref in request.background:
                item = {"ref": asdict(ref), "required": request.required}
                if item not in task["background"]:
                    task["background"].append(item)

        receipt = repo.mutate(
            scope,
            "tasks",
            task_id,
            request.expected_revision,
            request.request_id,
            asdict(request),
            change,
            initial=initial,
        )
        return {
            "task_id": task_id,
            **receipt,
            "task": self.task_view(repo.get(scope, "tasks", task_id), read),
        }

    @staticmethod
    def task_view(task: dict, read: ReadMemory) -> dict:
        from jiuwen_memory.common.errors import PermissionDeniedError

        visible = {k: v for k, v in task.items() if k not in {"receipts", "background"}}
        visible["background"] = []
        visible["background_partial"] = False
        for item in task["background"]:
            try:
                read(MemoryRef.parse(item["ref"]))
            except (NotFoundError, PermissionDeniedError, ConflictError):
                visible["background_partial"] = True
                continue
            visible["background"].append(item)
        return visible

    @staticmethod
    def binding(task: dict, binding_id: str) -> dict:
        for b in task["bindings"]:
            if b["binding_id"] == binding_id and b["active"]:
                return b
        raise ValidationError("binding is unavailable")

    def record(
        self,
        scope: Scope,
        event: ContinuityEvent,
        connector: str,
        read: ReadMemory,
        write: WriteMemory,
    ) -> dict:
        repo = self.repository
        key = fingerprint(
            {"connector": connector, "binding": event.binding_id, "event": event.event_id}
        )
        digest = fingerprint(asdict(event))
        with repo.lock:
            task = repo.get(scope, "tasks", event.task_id)
            binding = self.binding(task, event.binding_id)
            if task["state"] != "open" or binding["connector_id"] != connector:
                raise ConflictError("binding", message="inactive task or mismatched connection")
            for ref in event.evidence_refs:
                read(ref)
            try:
                receipt = repo.get(scope, "events", key)
            except NotFoundError:
                receipt = {
                    "digest": digest,
                    "status": "unknown",
                    "refs": [],
                    "connector": connector,
                    "task_id": event.task_id,
                }
                repo.save(scope, "events", key, receipt)
                try:
                    units = write(
                        event.content,
                        {
                            "continuity_kind": event.kind,
                            "continuity_classification_confidence": event.classification_confidence,
                            "continuity_task": event.task_id,
                            "continuity_event": key,
                            "continuity_connector": connector,
                            "continuity_duties": event.duty_ids,
                            "continuity_envelope": canonical(asdict(event)),
                        },
                    )
                except Exception:
                    return {"status": "unknown", "operation_id": key, "refs": []}
                receipt.update(status="partial", refs=[asdict(MemoryRef.of(u)) for u in units])
                repo.save(scope, "events", key, receipt)
            if receipt["digest"] != digest:
                raise ConflictError("event", message="event ID reused with different content")
            # Recheck all references even for retry; no cached private content escapes.
            for ref in receipt["refs"]:
                read(MemoryRef.parse(ref))
            # This is a recoverable two-record commit, not a cross-record transaction.
            # A crash after saving the task leaves a durable partial receipt with refs.
            # Retrying the same event reuses those refs (never rewrites content), deduplicates
            # background by value, then persists committed before returning success.
            # The task revision may advance again on recovery; it is monotonic, not a count
            # of unique events. Failure before refs are saved remains unknown and needs recovery.
            if receipt["refs"] and receipt["status"] != "committed":
                for ref in receipt["refs"]:
                    item = {"ref": ref, "required": False}
                    if item not in task["background"]:
                        task["background"].append(item)
                task["revision"] += 1
                repo.save(scope, "tasks", event.task_id, task)
                receipt["status"] = "committed"
                repo.save(scope, "events", key, receipt)
            if receipt["status"] == "committed" and self.associator is not None:
                if receipt.get("association_status") != "committed":
                    self._associate_event(scope, key, task, receipt, read, write)
            return {
                **{k: v for k, v in receipt.items() if k != "relation_candidates"},
                "index_status": "unchecked",
                "operation_id": key,
            }

    def _associate_event(self, scope, key, task, receipt, read, write):
        """Candidates are journaled before relation writes; retries reuse the same candidates."""
        try:
            if "relation_candidates" not in receipt:
                units = []
                for item in task["background"][-20:]:
                    try:
                        units.append(read(MemoryRef.parse(item["ref"])))
                    except (NotFoundError, PermissionDeniedError, ConflictError):
                        continue
                # IDs alone are only unambiguous within one Scope.
                units = [u for u in units if u.scope == scope]
                by_id = {u.id: u for u in units}
                candidates = []
                for found in self.associator.associate(units):
                    source, target = by_id[found.source_id], by_id[found.target_id]
                    rid = fingerprint(
                        {
                            "event": key,
                            "source": source.id,
                            "target": target.id,
                            "type": found.relation,
                        }
                    )
                    candidate = RelationRecord(
                        rid,
                        MemoryRef.of(source),
                        MemoryRef.of(target),
                        found.relation,
                        evidence_refs=[MemoryRef.of(source), MemoryRef.of(target)],
                        conditions=found.metadata.get("conditions", []),
                        conflict_kind=found.metadata.get("conflict_kind", ""),
                    )
                    candidates.append(asdict(candidate))
                receipt["relation_candidates"] = candidates
                self.repository.save(scope, "events", key, receipt)
            results = [
                self.relation(scope, RelationRecord.parse(raw), 0, read, write)
                for raw in receipt["relation_candidates"]
            ]
            receipt["association_status"] = (
                "committed" if all(r["status"] == "committed" for r in results) else "partial"
            )
        except Exception as exc:
            # Association is optional: do not turn a committed event into a failed write.
            # Keep diagnostics observable without logging source content or exception text.
            log = logger.warning if isinstance(exc, AgentMemoryError) else logger.error
            log("continuity association incomplete; error_type=%s", type(exc).__name__)
            receipt["association_status"] = "partial"
        self.repository.save(scope, "events", key, receipt)

    def relation(
        self,
        scope: Scope,
        relation: RelationRecord,
        expected_revision: int,
        read: ReadMemory,
        write: WriteMemory,
        read_previous: ReadMemory | None = None,
    ) -> dict:
        repo = self.repository
        if relation.revision != expected_revision + 1:
            raise ValidationError("relation revision must follow expected_revision")
        source = target = None
        if relation.status != RelationStatus.REVOKED:
            source, target = read(relation.source), read(relation.target)
            self.catalog.validate(relation, source, target)
            for ref in [*relation.evidence_refs, *relation.resolution_refs]:
                read(ref)
        elif expected_revision == 0:
            raise ValidationError("cannot revoke an unknown relation")
        payload = asdict(relation)
        digest = fingerprint(payload)
        with repo.lock:
            try:
                head = repo.get(scope, "relations", relation.relation_id)
            except NotFoundError:
                head = {"revision": 0, "history": []}
            if head.get("digest") == digest:
                if not head.get("ref"):
                    return {"status": "unknown", "relation_id": relation.relation_id}
                read(MemoryRef.parse(head["ref"]))
                return self._project(scope, relation, head, read)
            if head.get("revision") != expected_revision:
                raise ConflictError("relation", message="stale relation revision")
            if head.get("status") == "unknown":
                raise ConflictError("relation", message="unresolved write requires reconciliation")
            if head.get("ref"):
                import json

                previous = RelationRecord.parse(
                    json.loads((read_previous or read)(MemoryRef.parse(head["ref"])).content)
                )
                if (
                    previous.source.key != relation.source.key
                    or previous.target.key != relation.target.key
                    or previous.relation_type != relation.relation_type
                ):
                    raise ValidationError(
                        "new endpoints or relation type require a new relation ID"
                    )
                if previous.resolution_origin == "human" and not relation.resolution:
                    relation = replace(
                        relation,
                        resolution=previous.resolution,
                        resolution_reason=previous.resolution_reason,
                        resolution_origin="human",
                        resolution_refs=previous.resolution_refs,
                    )
                head["history"].append(head["ref"])
            resolved = self._resolve_conflict(scope, relation, source, target, read)
            payload = asdict(resolved)
            head.update(
                revision=relation.revision,
                digest=digest,
                status="unknown",
                ref=None,
                payload_digest=fingerprint(payload),
            )
            repo.save(scope, "relations", relation.relation_id, head)
            try:
                units = write(canonical(payload), {"continuity_kind": "relation"})
            except Exception:
                return {
                    "status": "unknown",
                    "relation_id": relation.relation_id,
                    "revision": relation.revision,
                }
            if len(units) != 1:
                raise ValidationError("relation writes require one unchanged memory record")
            if units[0].content != canonical(payload):
                raise ValidationError("construction changed the relation payload")
            head.update(ref=asdict(MemoryRef.of(units[0])), status="partial")
            repo.save(scope, "relations", relation.relation_id, head)
            return self._project(scope, relation, head, read)

    def _resolve_conflict(self, scope, relation, source, target, read):
        if relation.relation_type != "contradicts" or relation.status == RelationStatus.REVOKED:
            return relation
        if relation.resolution:
            # This path is the explicit, PEP-authorized review operation.
            return replace(relation, resolution_origin="human")
        if not self.conflict_resolver:
            return relation
        context = {"same_conditions": bool(relation.conditions), "evidence_complete": True}
        proof_refs = []
        for suffix, ref, unit in (("a", relation.source, source), ("b", relation.target, target)):
            context[f"source_{suffix}"] = unit.system_metadata.get("continuity_connector")
            context[f"time_{suffix}"] = unit.temporal.t_event
            verified = False
            conflicts = 0
            edges = self.graph.incident_edges(scope, ref.key, limit=101)
            if len(edges) > 100:
                context["evidence_complete"] = False
            for edge in edges[:100]:
                if edge.id == "continuity:relation:" + relation.relation_id:
                    continue
                try:
                    other, other_ref = self.resolve_edge(scope, edge.id, read)
                    for dependency in [other.source, other.target, *other.evidence_refs]:
                        read(dependency)
                except Exception:
                    context["evidence_complete"] = False
                    continue
                if other.status != RelationStatus.CONFIRMED:
                    continue
                if other.relation_type == "contradicts":
                    conflicts += 1
                if (
                    other.relation_type == "verifies"
                    and other.target == ref
                    and other.conditions == relation.conditions
                    and other.evidence_refs
                ):
                    verified = True
                    proof_refs.append(other_ref)
            context[f"verified_{suffix}"] = verified
            context["multiple_conflicts"] = (
                context.get("multiple_conflicts", False) or conflicts > 0
            )
        context["explicit_correction"] = any(
            u.system_metadata.get("continuity_kind") == "correction" for u in (source, target)
        )
        result = self.conflict_resolver.resolve(source, target, relation, context)
        return replace(
            relation,
            resolution="escalate" if result.candidate else result.decision.value,
            resolution_reason=result.reason,
            resolution_origin="model_candidate" if result.candidate else "automatic",
            resolution_refs=proof_refs if result.decision.value in {"prefer_a", "prefer_b"} else [],
        )

    def view(self, scope, request, read, discover):
        task = self.repository.get(scope, "tasks", request.task_id) if request.task_id else None
        return self.view_builder.build(
            scope,
            request,
            graph=self.graph,
            read=read,
            discover=discover,
            resolve_edge=lambda rid: self.resolve_edge(scope, rid, read),
            background=task["background"] if task else [],
            registry_revision=task["revision"] if task else None,
        )

    def _project(
        self, scope: Scope, relation: RelationRecord, head: dict, read: ReadMemory
    ) -> dict:
        try:
            unit = read(MemoryRef.parse(head["ref"]))
            self.projector.build([unit], mode=IndexWriteMode.RETRIEVAL_ONLY)
            head["status"] = "committed"
        except Exception:
            head["status"] = "partial"
        self.repository.save(scope, "relations", relation.relation_id, head)
        return {
            "status": head["status"],
            "relation_ref": head["ref"],
            "relation_id": relation.relation_id,
            "revision": relation.revision,
        }

    def resolve_edge(self, scope: Scope, relation_id: str, read: ReadMemory) -> tuple:
        import json

        relation_id = relation_id.removeprefix("continuity:relation:")
        head = self.repository.get(scope, "relations", relation_id)
        if not head.get("ref"):
            raise NotFoundError("relation")
        ref = MemoryRef.parse(head["ref"])
        unit = read(ref)
        relation = RelationRecord.parse(json.loads(unit.content))
        if relation.relation_id != relation_id or relation.revision != head["revision"]:
            raise ConflictError("relation", message="relation head changed")
        self.validate_resolution(scope, relation, read)
        return relation, ref

    def task_owner(self, scope, task_id):
        return self.repository.get(scope, "tasks", task_id)["owner"]

    def binding_connector(self, scope, task_id, binding_id):
        return self.binding(self.repository.get(scope, "tasks", task_id), binding_id)[
            "connector_id"
        ]

    def relation_head_ref(self, scope, relation_id):
        return self.repository.get(scope, "relations", relation_id).get("ref")

    def validate_resolution(self, scope, relation, read):
        import json

        for proof in relation.resolution_refs:
            unit = read(proof)
            verified = RelationRecord.parse(json.loads(unit.content))
            current = self.relation_head_ref(scope, verified.relation_id)
            if (
                current != asdict(proof)
                or verified.status != RelationStatus.CONFIRMED
                or verified.relation_type != "verifies"
            ):
                raise ConflictError("resolution", message="resolution evidence changed")

    def recovery_owner(self, scope, request):
        if request.kind == "event":
            receipt = self.repository.get(scope, "events", request.operation_id)
            return receipt.get("connector", "")
        return ""

    def recover(self, scope, request, read):
        import json

        with self.repository.lock:
            if any(ref.scope != scope for ref in request.refs):
                raise ValidationError("recovery references must belong to the target project")
            if request.kind == "event":
                receipt = self.repository.get(scope, "events", request.operation_id)
                contents = []
                for ref in request.refs:
                    unit = read(ref)
                    contents.append(unit.content)
                    envelope = json.loads(
                        str(unit.system_metadata.get("continuity_envelope", "{}"))
                    )
                    if (
                        unit.system_metadata.get("continuity_event") != request.operation_id
                        or fingerprint(envelope) != receipt["digest"]
                    ):
                        raise ConflictError("recovery", message="event receipt does not match")
                if "\n".join(contents) != envelope.get("content"):
                    raise ConflictError("recovery", message="incomplete event content")
                if receipt["refs"] and receipt["refs"] != [asdict(r) for r in request.refs]:
                    raise ConflictError("recovery", message="event already reconciled differently")
                if receipt["status"] != "committed":
                    receipt.update(refs=[asdict(r) for r in request.refs], status="partial")
                    self.repository.save(scope, "events", request.operation_id, receipt)
                return {
                    "status": receipt["status"],
                    "refs": receipt["refs"],
                    "next_action": "retry_original_event",
                }
            if len(request.refs) != 1:
                raise ValidationError("relation recovery requires exactly one record")
            head = self.repository.get(scope, "relations", request.operation_id)
            unit = read(request.refs[0])
            data = json.loads(unit.content)
            relation = RelationRecord.parse(data)
            if (
                relation.relation_id != request.operation_id
                or relation.revision != head["revision"]
                or fingerprint(data) != head.get("payload_digest")
            ):
                raise ConflictError("recovery", message="relation receipt does not match")
            if head.get("ref") and head["ref"] != asdict(request.refs[0]):
                raise ConflictError("recovery", message="relation already reconciled differently")
            head.update(ref=asdict(request.refs[0]), status="partial")
            self.repository.save(scope, "relations", request.operation_id, head)
            return self._project(scope, relation, head, read)

    def repair(self, scope: Scope, read: ReadMemory) -> dict:
        """Rebuild graph projections from authoritative relation heads, not re-extraction."""
        results = []
        incomplete = False
        with self.repository.lock:
            for head in self.repository.list(scope, "relations"):
                if not head.get("ref"):
                    incomplete = True
                    continue
                try:
                    import json

                    unit = read(MemoryRef.parse(head["ref"]))
                    relation = RelationRecord.parse(json.loads(unit.content))
                    if relation.status != RelationStatus.REVOKED:
                        for ref in [relation.source, relation.target, *relation.evidence_refs]:
                            read(ref)
                    results.append(self._project(scope, relation, head, read))
                except Exception:
                    incomplete = True
        return {
            "results": results,
            "coverage": "unknown",
            "gaps": ["reference_unavailable"] if incomplete else [],
        }


@ContinuityProducer.register("local")
def build(config):
    storage = StoreManagerProducer.resolve(config)
    repository = ContinuityRepository(
        storage.kv(config.get("registry_store", "default")),
        single_writer=config.get("single_writer", False),
        allow_ephemeral=config.get("allow_ephemeral", False),
    )
    from jiuwen_memory.retrieval.role_view import RoleViewProducer

    view = RoleViewProducer.build_named(config.get("role_view", "default"), config.ctx)

    def catalog_data(inline_key, path_key):
        if config.get(inline_key) and config.get(path_key):
            raise ValidationError("specify catalog inline or via file, not both")
        if config.get(path_key):
            from pathlib import Path

            import yaml

            data = yaml.safe_load(Path(config.get(path_key)).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValidationError("catalog file must contain a mapping")
            return data
        return config.get(inline_key, {})

    service = ContinuityService(
        repository,
        storage.graph(config.get("graph_store", "default")),
        view,
        projector=(
            IndexBuilderProducer.dep(config, "relation_index_builder")
            if config.get("relation_index_builder")
            else IndexBuilderProducer.build(
                "evidence_graph", {"graph_store": config.get("graph_store", "default")}, config.ctx
            )
        ),
        catalog=RelationCatalog(
            catalog_data("relation_types", "relation_types_path"),
            catalog_data("duty_policies", "duty_policies_path"),
        ),
        associator=(
            AssociatorProducer.build_named(config.get("relation_associator"), config.ctx)
            if config.get("relation_associator")
            else None
        ),
        conflict_resolver=(
            ConflictResolverProducer.build_named(config.get("conflict_resolver"), config.ctx)
            if config.get("conflict_resolver")
            else ConflictResolverProducer.build("chain", {}, config.ctx)
        ),
    )
    service.health()
    return service
