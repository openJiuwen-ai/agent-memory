"""Task/event/relation orchestration; caller supplies authorized memory operations."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime

from jiuwen_memory.common.errors import (
    AgentMemoryError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.relations.catalog import RelationCatalog
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.continuity import (
    MODEL_VERSION,
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
from jiuwen_memory.control.continuity import (
    Continuity,
    ContinuityProducer,
    ReadMemory,
    WriteMemory,
)
from jiuwen_memory.storage.graph import GraphStore
from jiuwen_memory.storage.store_manager import StoreManagerProducer
from jiuwen_memory.storage.types import IndexWriteMode

from . import sync
from .repository import ContinuityRepository

logger = get_logger(__name__)


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
        derivation_extractor=None,
        derivation_dedup=None,
        derivation_version="",
    ):
        if not graph.supports_incident_edges:
            raise ValidationError("continuity graph must support directed incident edges")
        projector_graph = projector.projection_graph()
        if projector_graph is not graph:
            raise ValidationError(
                "relation projector and the continuity view must share one GraphStore object"
            )
        if derivation_extractor is not None and not derivation_version:
            raise ValidationError("derivation requires an explicit builder/model version")
        self.derivation_extractor = derivation_extractor
        self.derivation_dedup = derivation_dedup
        self.derivation_version = derivation_version
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
        if self.derivation_extractor:
            self.derivation_extractor.health()
        if self.derivation_dedup:
            self.derivation_dedup.health()

    def close(self) -> None:
        """Stop the Registry writer; injected stores remain owned by their assembly root."""
        self.repository.close()

    def task(self, scope: Scope, request: TaskRequest, owner: str, read: ReadMemory) -> dict:
        repo = self.repository
        if request.operation == "find":
            matches = []
            for task in repo.list(scope, "tasks"):
                for binding in task["bindings"]:
                    if not binding["active"]:
                        continue
                    if binding["connector_id"] != request.connector_id:
                        continue
                    if binding["context_id"] == request.context_id:
                        matches.append({"task_id": task["task_id"], "revision": task["revision"]})
                        break
            return {"tasks": matches}
        if request.operation == "get":
            task = repo.get(scope, "tasks", request.task_id)
            view = self.task_view(task, read)
            view["source_cursors"] = {
                binding["binding_id"]: sync.cursor(
                    repo, scope, binding["connector_id"], binding["context_id"]
                ) for binding in task["bindings"] if binding["active"]
            }
            return view
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
        *,
        query_output=None,
        conditional_update=None,
    ) -> dict:
        if self.derivation_extractor is not None and query_output is None:
            raise ValidationError("derivation requires exact read-after-write metadata lookup")
        repo = self.repository
        key = fingerprint(
            {"connector": connector, "binding": event.binding_id, "event": event.event_id}
        )
        envelope = asdict(event)
        if event.source_position is None:
            # Preserve hashes of envelopes written before ordered-source support.
            envelope.pop("source_position")
            envelope.pop("previous_position")
        digest = fingerprint(envelope)
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
                # After a cutover the source stops emitting events entirely; ordered and
                # unordered new events alike must be rejected, while replay of an already
                # committed receipt continues to resolve above.
                sync.reject_cutover(repo, scope, connector, binding["context_id"])
                if event.source_position is not None:
                    sync.reserve(
                        repo, scope, connector, binding["context_id"], event, key, digest
                    )
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
                            "continuity_envelope": canonical(envelope),
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
            if receipt["status"] == "committed" and self.derivation_extractor is not None:
                self._derive_event(
                    scope, key, task, receipt, read, write, query_output, conditional_update
                )
            if receipt["status"] == "committed" and self.associator is not None:
                if receipt.get("association_status") != "committed":
                    self._associate_event(scope, key, task, receipt, read, write)
            for item in receipt.get("derived", []):
                read(MemoryRef.parse(item["ref"]))
            source_cursor = None
            if event.source_position is not None:
                source_cursor = sync.cursor(repo, scope, connector, binding["context_id"])
                if receipt["status"] == "committed" and all(
                    receipt.get(stage, "committed") == "committed"
                    for stage in ("derivation_status", "association_status")
                ):
                    source_cursor = sync.commit(
                        repo, scope, connector, binding["context_id"], event, key, digest
                    )
            return {
                **{
                    k: v
                    for k, v in receipt.items()
                    if k not in {"relation_candidates", "build_plan"}
                },
                "index_status": "unchecked",
                "operation_id": key,
                "source_cursor": source_cursor,
            }

    def _derive_event(
        self, scope, key, task, receipt, read, write, query_output, conditional_update
    ):
        """Journal a side-effect-free extraction plan before applying its outputs.

        Called under repository.lock: concurrent retries of one output cannot both pass
        the lookup/create boundary. Content is written only via the authorized callback.
        The built-in planner reuses exact, authorized snapshots; semantic updates remain
        explicit corrections rather than silently overwriting another author's memory.
        """
        from jiuwen_memory.common.type_def import MemoryUnit, Modality, Segment

        from .derivation import DerivationItem, DerivationPlan, reconcile

        repo = self.repository
        try:
            sources = [read(MemoryRef.parse(ref)) for ref in receipt["refs"]]
            build_id = fingerprint({"event": key, "version": self.derivation_version})
            source_refs_digest = canonical(receipt["refs"])
            if "build_plan" not in receipt or receipt.get("build_id") != build_id:
                # A derivation_version change is an explicit new-version rebuild: re-plan
                # against the same sources and update the previous outputs in place (CAS)
                # instead of duplicating them under a fresh build. The batch id and the
                # frozen plan are saved together only after extraction succeeds, so a failed
                # model run never leaves the new id pointing at a stale plan.
                extracted = self.derivation_extractor.extract(sources)
                plan = []
                seen = set()
                assigned_refs: set[str] = set()
                for unit in extracted:
                    if unit.scope != scope:
                        raise ValidationError("derivation cannot change source scope")
                    # The writer stores plain text; freeze exactly that representation.
                    digest = MemoryRef.of(
                        MemoryUnit(
                            id="planned", scope=scope, segments=[Segment(content=unit.content)]
                        )
                    ).content_digest
                    if digest in seen:
                        continue
                    seen.add(digest)
                    item = DerivationItem(
                        output_key=f"{build_id}:{len(plan)}",
                        action="created",
                        content=unit.content,
                        content_digest=digest,
                        event_key=key,
                    )
                    if self.derivation_dedup is not None:
                        for candidate, _ in self.derivation_dedup.recall(unit):
                            ref = MemoryRef.of(candidate)
                            if ref.scope != scope or candidate.system_metadata.get(
                                "continuity_event"
                            ):
                                continue
                            if ref.unit_id in assigned_refs:
                                # Each previous output revises at most one old record; a
                                # second output matching the same target must not freeze a
                                # duplicate update of the same digest.
                                continue
                            try:
                                authorized = read(ref)
                            except (PermissionDeniedError, NotFoundError, ConflictError):
                                continue
                            if MemoryRef.of(authorized).content_digest == digest:
                                item = replace(item, action="reused", ref=ref)
                                assigned_refs.add(ref.unit_id)
                                break
                            # A previous derived output of the same sources with different
                            # content is a revised statement: update it in place (CAS).
                            plain_text_target = (
                                len(authorized.segments) == 1
                                and not authorized.segments[0].assets
                                and authorized.segments[0].source == Modality.TEXT
                            )
                            if (
                                plain_text_target
                                and candidate.system_metadata.get("continuity_build")
                                and candidate.system_metadata.get("continuity_source_refs")
                                == source_refs_digest
                            ):
                                item = replace(
                                    item,
                                    action="updated",
                                    ref=ref,
                                    digest_before=MemoryRef.of(authorized).content_digest,
                                    digest_after=digest,
                                )
                                assigned_refs.add(ref.unit_id)
                                break
                    plan.append(asdict(item))
                receipt["build_id"] = build_id
                receipt["derivation_status"] = "pending"
                receipt["build_plan"] = plan
                # A new plan supersedes the previous build's outputs; a later mid-reconcile
                # failure must not leave the stale derived list behind for re-read checks.
                receipt["derived"] = []
                receipt["derivation_conflicts"] = []
                repo.save(scope, "events", key, receipt)
            items = [
                DerivationItem(
                    **{**item, "ref": MemoryRef.parse(item["ref"]) if item.get("ref") else None}
                )
                for item in receipt["build_plan"]
            ]

            def create(item):
                units = write(
                    item.content,
                    {
                        "continuity_build": receipt["build_id"],
                        "continuity_output": item.output_key,
                        "continuity_connector": receipt["connector"],
                        "continuity_source_refs": canonical(receipt["refs"]),
                        "continuity_task": receipt["task_id"],
                        "continuity_kind": "statement",
                        "continuity_classification_confidence": "unknown",
                    },
                )
                if len(units) != 1 or MemoryRef.of(units[0]).content_digest != item.content_digest:
                    raise ConflictError("derivation", message="writer changed frozen output")
                return units[0]

            def add_source(event_key, unit_id):
                repo.save(
                    scope,
                    "derivation_sources",
                    fingerprint([event_key, unit_id]),
                    {
                        "event": event_key,
                        "unit_id": unit_id,
                        "source_refs": receipt["refs"],
                    },
                )

            result = reconcile(
                DerivationPlan(receipt["build_id"], items),
                query_output=query_output,
                read=read,
                create=create,
                add_source=add_source,
                conditional_update=conditional_update,
            )
            receipt.update(
                derivation_status=result.status,
                derived=result.derived,
                derivation_conflicts=result.conflicts,
            )
            for derived in result.derived:
                background = {"ref": derived["ref"], "required": False}
                if background not in task["background"]:
                    task["background"].append(background)
                    task["revision"] += 1
            repo.save(scope, "tasks", receipt["task_id"], task)
        except Exception as exc:
            receipt["derivation_status"] = "partial"
            logger.error("continuity derivation incomplete; error_type=%s", type(exc).__name__)
        repo.save(scope, "events", key, receipt)

    def _associate_event(self, scope, key, task, receipt, read, write):
        """Candidates are journaled before relation writes; retries reuse the same candidates.

        The injected associator may emit catalog-typed relations directly (``evidence_llm``)
        or legacy association types (old LLMAssociator's ``caused_by`` / ``contradicts``).
        Catalog-typed relations become candidates verbatim; legacy relations are mapped
        through :func:`resolve_discovered_conflicts` so ``contradicts`` receives an injected
        ConflictResolver recommendation while staying ``candidate`` — never auto-confirmed.
        """
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
                discovered = self.associator.associate(units)
                catalog_typed = [r for r in discovered if r.relation in self.catalog.types]
                legacy = [r for r in discovered if r.relation not in self.catalog.types]

                candidates = []
                for found in catalog_typed:
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

                if legacy:
                    mapped = self._map_legacy_relations(scope, key, legacy, by_id, read)
                    candidates.extend(asdict(record) for record in mapped)
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

    def _map_legacy_relations(self, scope, key, legacy, by_id, read):
        """Map legacy association types to candidate evidence relations via the pipeline."""
        from jiuwen_memory.common.relations.legacy import evidence_candidate

        from ...construction.conflict_resolver_impl.pipeline import resolve_discovered_conflicts

        references = {u.id: MemoryRef.of(u) for u in by_id.values()}
        by_key = {MemoryRef.of(u).key: MemoryRef.of(u) for u in by_id.values()}

        def supports(ref_key):
            out = []
            for edge in self.graph.incident_edges(scope, ref_key, limit=101):
                try:
                    other, other_ref = self.resolve_edge(scope, edge.id, read)
                except AgentMemoryError as exc:
                    logger.warning(
                        "Relation support unavailable: error_type=%s", type(exc).__name__
                    )
                    continue
                if (
                    other.relation_type == "supports"
                    and other.status == RelationStatus.CONFIRMED
                ):
                    out.append((other, other_ref))
            return out

        def sources(ref_key):
            ref = by_key.get(ref_key)
            if ref is None:
                return ""
            try:
                return read(ref).system_metadata.get("continuity_connector", "")
            except Exception:
                return ""

        def times(ref_key):
            ref = by_key.get(ref_key)
            if ref is None:
                return None
            try:
                return read(ref).temporal.t_event
            except Exception:
                return None

        if self.conflict_resolver is not None:
            return resolve_discovered_conflicts(
                legacy,
                references,
                self.conflict_resolver,
                supports=supports,
                read=read,
                sources=sources,
                times=times,
                batch_id=key,
            )
        # No injected resolver: map the mappable subset without an adjudication step.
        records = []
        for index, relation in enumerate(legacy):
            if relation.relation not in {"caused_by", "contradicts"}:
                continue
            record = evidence_candidate(
                relation, references, relation_id=f"assoc-{index}", batch_id=key
            )
            if record is not None:
                records.append(record)
        return records

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
            previous_ref = head.get("ref")
            if head.get("digest") == digest:
                if not previous_ref:
                    return {"status": "unknown", "relation_id": relation.relation_id}
                read(MemoryRef.parse(previous_ref))
                return self._project(scope, relation, head, read)
            if head.get("revision") != expected_revision:
                raise ConflictError("relation", message="stale relation revision")
            if head.get("status") == "unknown":
                raise ConflictError("relation", message="unresolved write requires reconciliation")
            if previous_ref:
                import json

                previous = RelationRecord.parse(
                    json.loads((read_previous or read)(MemoryRef.parse(previous_ref)).content)
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
                head["history"].append(previous_ref)
            resolved = self._resolve_conflict(scope, relation, source, target, read)
            payload = asdict(resolved)
            dependencies = [
                resolved.source, resolved.target,
                *resolved.evidence_refs, *resolved.resolution_refs,
            ]
            head.update(
                revision=relation.revision,
                digest=digest,
                status="unknown",
                ref=None,
                payload_digest=fingerprint(payload),
                relation_state=resolved.status.value,
                dependencies=[asdict(ref) for ref in dependencies],
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
                # Only same-condition, confirmed, evidenced positive support selects a
                # winner. "verifies" records that a test ran, not whether the result
                # supports or contradicts the claim under test, so it never counts here.
                matching_evidence = other.conditions == relation.conditions and other.evidence_refs
                if (
                    other.relation_type == "supports"
                    and other.target == ref
                    and matching_evidence
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
        result = self.view_builder.build(
            scope,
            request,
            graph=self.graph,
            read=read,
            discover=discover,
            resolve_edge=lambda rid: self.resolve_edge(scope, rid, read),
            background=task["background"] if task else [],
            registry_revision=task["revision"] if task else None,
        )
        result["meta"]["relation_coverage"] = self.relation_coverage(scope)
        return result

    def relation_coverage(self, scope: Scope) -> str:
        """Coverage of the registered relation build range, not ingestion completeness.

        A manifest is maintained for audit: it records the relation model version, the
        registered source set digest, the projection watermark and per-status counts.
        ``covered_for_requested_scope`` only means every registered relation truth has a
        committed projection; it never proves that all history was ingested.
        """
        heads = self.repository.list(scope, "relations")
        if not heads:
            return "unknown"
        projected = sum(1 for h in heads if h.get("ref") and h.get("status") == "committed")
        status = "covered_for_requested_scope" if projected == len(heads) else "partial"
        self.repository.save(
            scope,
            "coverage",
            "relations",
            {
                "model_version": MODEL_VERSION,
                "relation_count": len(heads),
                "projected_count": projected,
                "projection_watermark": max((h.get("revision", 0) for h in heads), default=0),
                "source_digest": fingerprint(sorted(h.get("digest", "") for h in heads)),
                "status": status,
            },
        )
        return status

    def _project(
        self, scope: Scope, relation: RelationRecord, head: dict, read: ReadMemory
    ) -> dict:
        try:
            unit = read(MemoryRef.parse(head["ref"]))
            self.projector.build([unit], mode=IndexWriteMode.RETRIEVAL_ONLY)
            head["status"] = "committed"
            # Repair older heads as well, so retention no longer has to retain all
            # evidence conservatively when an old record lacks its dependency manifest.
            head["relation_state"] = relation.status.value
            dependencies = [
                relation.source, relation.target,
                *relation.evidence_refs, *relation.resolution_refs,
            ]
            head["dependencies"] = [asdict(ref) for ref in dependencies]
            self._journal_change(scope, relation)
        except Exception:
            head["status"] = "partial"
        self.repository.save(scope, "relations", relation.relation_id, head)
        return {
            "status": head["status"],
            "relation_ref": head["ref"],
            "relation_id": relation.relation_id,
            "revision": relation.revision,
        }

    def _change_watermark(self, scope: Scope) -> dict:
        try:
            return self.repository.get(scope, "changes", "watermark")
        except NotFoundError:
            return {"next": 0}

    def _journal_change(self, scope: Scope, relation: RelationRecord) -> None:
        """Append a committed relation revision/revocation to the change journal (idempotent).

        The change record and the watermark are two separate KV records, so a crash between
        their saves leaves a record whose id is not yet reflected in the watermark. A new id
        is therefore derived from the journal itself (max recorded id + 1), not only the
        watermark: recovery is order-independent and a later change never reuses an id that
        an earlier, not-yet-watermarked change already claimed.
        """
        key = fingerprint(
            {"relation_id": relation.relation_id, "revision": relation.revision}
        )
        try:
            existing = self.repository.get(scope, "changes", key)
            watermark = self._change_watermark(scope)
            if watermark["next"] <= existing["change_id"]:
                watermark["next"] = existing["change_id"] + 1
                self.repository.save(scope, "changes", "watermark", watermark)
            return  # idempotent: this change's id is already durable in its record
        except NotFoundError:
            pass
        watermark = self._change_watermark(scope)
        change_id = watermark["next"]
        for record in self.repository.list(scope, "changes"):
            if "change_id" in record:
                change_id = max(change_id, record["change_id"] + 1)
        self.repository.save(
            scope,
            "changes",
            key,
            {
                "change_id": change_id,
                "relation_id": relation.relation_id,
                "revision": relation.revision,
                "relation_type": relation.relation_type,
                "status": relation.status.value,
                "source_key": relation.source.key,
                "target_key": relation.target.key,
                "changed_at": datetime.now(UTC).isoformat(),
            },
        )
        watermark["next"] = change_id + 1
        self.repository.save(scope, "changes", "watermark", watermark)

    def review_changes(self, scope: Scope, since: int = 0) -> dict:
        """Return relation changes since ``since`` and the new watermark.

        The change journal is the durable "变化入库" leg of the review loop: callers
        re-derive review items only from changes after the watermark they last processed.
        """
        # Snapshot records and cursor under the writer lock. A durable record may
        # precede its watermark after a failed save, even before any writer retries.
        with self.repository.lock:
            watermark = self._change_watermark(scope)
            changes = [r for r in self.repository.list(scope, "changes") if "change_id" in r]
            next_id = max([watermark["next"], *(c["change_id"] + 1 for c in changes)])
            changes = [c for c in changes if c["change_id"] >= since]
            changes.sort(key=lambda c: c["change_id"])
            return {"changes": changes, "watermark": next_id}

    def propagate_review(
        self,
        scope: Scope,
        since: int,
        read: ReadMemory,
        *,
        required: set[str],
        max_nodes: int = 50,
        max_depth: int = 4,
    ) -> dict:
        """Incrementally propagate review from the change journal into three tiers.

        Each changed relation seeds a bounded forward walk of the dependent subgraph
        (``review.review_impact``), so only the affected references are re-reviewed, never
        the whole graph. Returns the aggregated tiers plus the watermark to resume from.
        """
        from .review import review_impact

        reviewed = self.review_changes(scope, since)
        required_items: list[str] = []
        candidate_items: list[str] = []
        unchecked = False
        for change in reviewed["changes"]:
            for seed in {change["source_key"], change["target_key"]}:
                outcome = review_impact(
                    scope,
                    self.graph,
                    resolve_edge=lambda rid: self.resolve_edge(scope, rid, read),
                    seed=seed,
                    required=required,
                    max_nodes=max_nodes,
                    max_depth=max_depth,
                )
                required_items.extend(outcome["required"])
                candidate_items.extend(outcome["candidate"])
                unchecked = unchecked or bool(outcome["gaps"])
        return {
            "required": sorted(set(required_items)),
            "candidate": sorted(set(candidate_items) - set(required_items)),
            "gaps": ["unchecked_dependencies"] if unchecked else [],
            "watermark": reviewed["watermark"],
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

    def retains(self, unit) -> bool:
        """Check all project registries so a cross-scope reference retains its real source."""
        if unit.system_metadata.get("continuity_kind") == "relation":
            return True
        key = MemoryRef.of(unit).key
        with self.repository.lock:
            for project in self.repository.kv.scopes():
                for task in self.repository.list(project, "tasks"):
                    if task.get("state") == "open" and any(
                        MemoryRef.parse(item["ref"]).key == key
                        for item in task.get("background", [])
                    ):
                        return True
                for head in self.repository.list(project, "relations"):
                    if head.get("relation_state") == "revoked":
                        continue
                    if "dependencies" not in head:
                        # Legacy control rows must be backfilled before cleanup is safe.
                        return True
                    if any(MemoryRef.parse(ref).key == key for ref in head["dependencies"]):
                        return True
        return False

    def is_referenced(self, scope: Scope, ref: MemoryRef, read: ReadMemory | None = None) -> bool:
        """Whether ``ref`` is still referenced by an active task background or a relation.

        Retention guard for the lifecycle sweep: evidence still cited by an open task's
        background, or by any relation's endpoints/evidence/resolution, must not be cleaned
        up early. ``read`` enables the relation-truth scan; without it only the cheap
        active-task check runs.
        """
        for task in self.repository.list(scope, "tasks"):
            if task.get("state") != "open":
                continue
            if any(item.get("ref") == asdict(ref) for item in task.get("background", [])):
                return True
        if read is None:
            return False
        import json

        for head in self.repository.list(scope, "relations"):
            if not head.get("ref"):
                continue
            try:
                unit = read(MemoryRef.parse(head["ref"]))
                relation = RelationRecord.parse(json.loads(unit.content))
            except (AgentMemoryError, ValueError, TypeError, KeyError) as exc:
                # An unreadable relation cannot prove absence of dependencies.
                logger.warning("Retention check incomplete: error_type=%s", type(exc).__name__)
                return True
            if ref in [
                relation.source,
                relation.target,
                *relation.evidence_refs,
                *relation.resolution_refs,
            ]:
                return True
        return False

    def quarantine_source(
        self, scope: Scope, source_key: str, reason: str, *, operator: str = ""
    ) -> dict:
        """Quarantine a source range with an auditable reason.

        A quarantined source is read as unavailable by every guard (get / search /
        role view / handoff); derived content that cites it is blocked rather than
        silently summarized around. The record persists until ``release_source``.
        """
        if not source_key or not reason.strip():
            raise ValidationError("source key and a non-empty reason are required")
        record = {
            "source_key": source_key,
            "reason": reason,
            "operator": operator,
            "quarantined_at": datetime.now(UTC).isoformat(),
        }
        self.repository.save(scope, "sources", source_key, record)
        return record

    def source_quarantine(self, scope: Scope, source_key: str) -> dict | None:
        try:
            return self.repository.get(scope, "sources", source_key)
        except NotFoundError:
            return None

    def release_source(self, scope: Scope, source_key: str) -> bool:
        """Release a quarantined source; returns whether it was quarantined."""
        if self.source_quarantine(scope, source_key) is None:
            return False
        self.repository.delete(scope, "sources", source_key)
        return True

    def mark_source_cutover(
        self, scope: Scope, connector: str, context_id: str
    ) -> dict:
        """Cut a dual-write source over to agent-memory as the single authoritative writer.

        Records the cutover watermark at the caught-up cursor position; a pending source
        event must be settled first, and afterwards the source must stop emitting events.
        Held under the same repository lock as ``record`` so a concurrent event commit
        cannot advance the cursor between the watermark read and its write.
        """
        with self.repository.lock:
            return sync.mark_cutover(self.repository, scope, connector, context_id)

    def source_cutover(self, scope: Scope, connector: str, context_id: str) -> int | None:
        """Return the cutover watermark position for an ordered source, or ``None``."""
        return sync.cutover(self.repository, scope, connector, context_id)

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
            supported = RelationRecord.parse(json.loads(unit.content))
            current = self.relation_head_ref(scope, supported.relation_id)
            if (
                current != asdict(proof)
                or supported.status != RelationStatus.CONFIRMED
                or supported.relation_type != "supports"
            ):
                raise ConflictError("resolution", message="resolution evidence changed")

    def recovery_owner(self, scope, request):
        if request.kind in {"event", "derivation"}:
            receipt = self.repository.get(scope, "events", request.operation_id)
            return receipt.get("connector", "")
        return ""

    def recover(self, scope, request, read):
        import json

        with self.repository.lock:
            if any(ref.scope != scope for ref in request.refs):
                raise ValidationError("recovery references must belong to the target project")
            if request.kind == "derivation":
                receipt = self.repository.get(scope, "events", request.operation_id)
                if "build_plan" not in receipt:
                    raise ValidationError("no frozen derivation plan to reconcile")
                for source in receipt["refs"]:
                    read(MemoryRef.parse(source))
                build_id = receipt["build_id"]
                # created outputs carry this build's identity; reused/updated targets are
                # located by their node key (scope + unit id), so recovery is action-aware
                # and does not mistake an in-place update for a different build's output.
                by_output = {}
                by_target = {}
                for item in receipt["build_plan"]:
                    by_output[item["output_key"]] = item
                    if item["action"] in {"updated", "reused"} and item.get("ref"):
                        by_target[MemoryRef.parse(item["ref"]).key] = item
                supplied = {}
                for ref in request.refs:
                    unit = read(ref)
                    output = unit.system_metadata.get("continuity_output")
                    # A new writer retags updated targets, while legacy successful
                    # writes may retain old tags. Target + frozen digest identifies both.
                    item = by_target.get(MemoryRef.of(unit).key)
                    if item is not None:
                        expected = (
                            item["digest_after"] if item["action"] == "updated"
                            else MemoryRef.parse(item["ref"]).content_digest
                        )
                        if MemoryRef.of(unit).content_digest != expected:
                            raise ConflictError(
                                "derivation", message="target differs from frozen reference"
                            )
                    else:
                        item = by_output.get(output)
                        if (unit.system_metadata.get("continuity_build") != build_id
                                or item is None or item["action"] != "created"):
                            raise ConflictError(
                                "derivation", message="output is not in frozen plan"
                            )
                        if MemoryRef.of(unit).content_digest != item["content_digest"]:
                            raise ConflictError("derivation", message="output digest changed")
                    if item["output_key"] in supplied:
                        raise ConflictError("derivation", message="duplicate output identity")
                    supplied[item["output_key"]] = unit
                derived = []
                for item in receipt["build_plan"]:
                    unit = supplied.get(item["output_key"])
                    if unit is not None:
                        derived.append(
                            {
                                "output_key": item["output_key"],
                                "action": item["action"],
                                "ref": asdict(MemoryRef.of(unit)),
                            }
                        )
                # This entry only verifies supplied outputs; the original-event retry
                # performs remaining writes and task-background association.
                receipt["derived"] = derived
                receipt["derivation_status"] = "partial"
                self.repository.save(scope, "events", request.operation_id, receipt)
                return {
                    "status": "partial",
                    "derived": derived,
                    "next_action": "retry_original_event",
                }
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
            "coverage": self.relation_coverage(scope),
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
    try:
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

        from jiuwen_memory.construction.dedup import DedupProducer
        from jiuwen_memory.construction.extractor import ExtractorProducer

        service = ContinuityService(
            repository,
            storage.graph(config.get("graph_store", "default")),
            view,
            projector=(
                IndexBuilderProducer.dep(config, "relation_index_builder")
                if config.get("relation_index_builder")
                else IndexBuilderProducer.build(
                    "evidence_graph",
                    {"graph_store": config.get("graph_store", "default")},
                    config.ctx,
                )
            ),
            derivation_extractor=(
                ExtractorProducer.build_named(config.get("derivation_extractor"), config.ctx)
                if config.get("derivation_extractor")
                else None
            ),
            derivation_dedup=(
                DedupProducer.build_named(config.get("derivation_dedup"), config.ctx)
                if config.get("derivation_dedup")
                else None
            ),
            derivation_version=config.get("derivation_version", ""),
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
    except BaseException:
        repository.close()
        raise
