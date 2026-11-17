"""Task/event/relation orchestration; caller supplies authorized memory operations."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime

from jiuwen_memory.common.errors import (
    AgentMemoryError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.relations.catalog import RelationCatalog
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.continuity import (
    MODEL_VERSION,
    ContinuityEvent,
    MemoryRef,
    RelationRecord,
    RelationStatus,
    TaskRequest,
    canonical,
    fingerprint,
    view_kind,
    view_status,
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

from .projection import IndexBuilderRelationProjector
from .repository import ContinuityRepository
from .task_search import TaskSearchIndex

logger = get_logger(__name__)


def _is_relation_head(record) -> bool:
    """True for a relation *head*; False for an immutable ``<id>/rev/<n>`` revision row.

    Revisions live under ``/continuity/relations/<id>/rev/<n>`` so a plain prefix scan
    returns them too. They carry neither ``current`` (KV head) nor ``ref`` (legacy head),
    which is exactly what separates them from heads.
    """
    return "current" in record or "ref" in record



_DISCOVERY_ORDER = ("background", "anchor", "relation", "semantic_candidate")


# S11 ViewAnnotation 的完整字段；两档都给出全部键，接续档没有的值用空值而不是省略，
# 客户端不必按档位分支解析。
_ANNOTATION_DEFAULTS = {
    "kind": "statement",
    "status": "unknown",
    "discovery": "",
    "required": False,
    "classification_confidence": "unknown",
    "evidence_refs": [],
    "conditions": [],
    "duty_ids": [],
    "valid_duties": [],
    "common": False,
    "path_ids": [],
}


def _view_entry(*, unit_id: str, text: str, **annotation) -> dict:
    """One normalized view entry (S11 ViewAnnotation, both modes).

    Only S11 fields are kept: the builder's own ``id``/``text`` are entry-level data, not
    annotation, and must not be duplicated into it.
    """
    full = {}
    for key, default in _ANNOTATION_DEFAULTS.items():
        value = annotation.get(key)
        if value is None:
            value = default
        full[key] = list(value) if isinstance(default, list) else value
    return {"unit_id": unit_id, "text": text, "annotation": full}


def _unit_entry(unit: MemoryUnit, discovery: str, *, required: bool = False) -> dict:
    """Continuity-mode entry for a unit read directly (anchor / background)."""
    event_kind = str(unit.system_metadata.get("continuity_kind", "statement"))
    return _view_entry(
        unit_id=unit.id,
        text=unit.content,
        kind=view_kind(event_kind),
        status=view_status(event_kind),
        discovery=discovery,
        required=required,
        classification_confidence=unit.system_metadata.get(
            "continuity_classification_confidence", "unknown"
        ),
        evidence_refs=[asdict(MemoryRef.of(unit))],
        duty_ids=list(unit.system_metadata.get("continuity_duties", []) or []),
        common=True,
    )


def _finalize_view(items: list[dict], *, top_k: int, gaps: list[dict]) -> list[dict]:
    """Order by discovery source (background → anchor → relation → semantic) then truncate.

    Ordering matters for truncation: required background must never be pushed out by
    semantic candidates, so it is sorted first and the truncation gap is appended after.
    """
    ranked = sorted(
        items,
        key=lambda entry: _DISCOVERY_ORDER.index(entry["annotation"]["discovery"])
        if entry["annotation"]["discovery"] in _DISCOVERY_ORDER
        else len(_DISCOVERY_ORDER),
    )
    if len(ranked) > top_k:
        gaps.append(
            {
                "code": "budget_exhausted",
                "message": f"view returned {len(ranked)} items; truncated to top_k",
                "blocking": False,
                "affected_item_ids": [],
            }
        )
        ranked = ranked[:top_k]
    return ranked


class ContinuityService(Continuity):
    def __init__(
        self,
        repository: ContinuityRepository,
        graph: GraphStore | None = None,
        view_builder=None,
        *,
        mode: str = "role",
        projector=None,
        catalog=None,
        conflict_resolver=None,
        associator=None,
        derivation_extractor=None,
        derivation_dedup=None,
        derivation_version="",
        relation_projector=None,
        connectors: dict | None = None,
        task_embedder=None,
        task_tokenizer=None,
    ):
        # 关系投影走独立端口（接收 RelationRecord）；没有专门投影器时用适配器包住
        # 既有的 IndexBuilder，伪造 MemoryUnit 的知识只留在 projection.py 一处。
        if relation_projector is None and projector is not None:
            relation_projector = IndexBuilderRelationProjector(projector)
        self.relation_projector = relation_projector
        # 可信连接配置由装配注入；``None`` 表示「未知」，此时带连接的关系一律不可用
        # （fail closed），不允许「没配置就放行」。
        self.connectors = connectors

        if mode not in {"continuity", "role"}:
            raise ValidationError("continuity mode must be 'continuity' or 'role'")
        if mode == "role":
            if graph is None or not graph.supports_incident_edges:
                raise ValidationError(
                    "role mode requires a graph supporting directed incident edges"
                )
            if view_builder is None:
                raise ValidationError("role mode requires a role view builder")
            if projector is None:
                raise ValidationError("role mode requires a relation projector")
            projector_graph = projector.projection_graph()
            if projector_graph is not graph:
                raise ValidationError(
                    "relation projector and the continuity view must share one GraphStore object"
                )
        if derivation_extractor is not None and not derivation_version:
            raise ValidationError("derivation requires an explicit builder/model version")
        self.mode = mode
        self.derivation_extractor = derivation_extractor
        self.derivation_dedup = derivation_dedup
        self.derivation_version = derivation_version
        self.catalog = catalog or RelationCatalog()
        self.projector = projector
        self.conflict_resolver = conflict_resolver
        self.associator = associator
        if associator is not None:
            associator.configure_relations(self.catalog)
        if view_builder is not None:
            view_builder.configure_relations(self.catalog)
        self.view_builder = view_builder
        self.repository = repository
        self.graph = graph
        self.task_search = TaskSearchIndex(task_embedder, task_tokenizer)

    def health(self) -> dict:
        self.repository.kv.health()
        if self.mode == "role":
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
        return {"continuity_mode": self.mode}

    def _require_role_mode(self) -> None:
        if self.mode != "role":
            raise UnsupportedCapabilityError(
                capability="relation",
                value=self.mode,
                component="continuity",
                message=f"continuity {self.mode} mode has no relation capability",
            )

    def close(self) -> None:
        """Stop the Registry writer; injected stores remain owned by their assembly root."""
        self.repository.close()

    def task(self, scope: Scope, request: TaskRequest, owner: str, read: ReadMemory) -> dict:
        repo = self.repository
        if request.operation == "find":
            matches = []
            tasks = repo.list(scope, "tasks")
            for task in tasks:
                if task.get("state") != "open":
                    continue
                for binding in task["bindings"]:
                    if not binding["active"]:
                        continue
                    if binding["connector_id"] != request.connector_id:
                        continue
                    if binding["context_id"] == request.context_id:
                        matches.append({"task_id": task["task_id"], "revision": task["revision"],
                                        "title": task.get("title", ""),
                                        "description": task.get("description", ""),
                                        "state": task["state"], "match_type": "context_exact"})
                        break
            if matches or not (request.query or "").strip():
                return {"tasks": matches}
            return {"tasks": self.task_search.search(
                scope, tasks, request.query.strip(), request.top_k,
            )}
        if request.operation == "get":
            task = repo.get(scope, "tasks", request.task_id)
            view = self.task_view(task, read)
            return view
        task_id = request.task_id
        initial = None
        if request.operation == "create":
            task_id = fingerprint({"owner": owner, "request_id": request.request_id})
            parent = (repo.get(scope, "tasks", request.parent_task_id)
                      if request.parent_task_id else None)
            initial = dict(
                task_id=task_id, owner=owner, revision=0, state="open", bindings=[], background=[],
                title=request.title.strip(), description=request.description.strip(),
                parent_task_id=request.parent_task_id,
                root_task_id=(parent.get("root_task_id") or parent["task_id"])
                if parent else task_id,
            )
        for ref in request.background:
            read(ref)

        def change(task: dict) -> None:
            if task["state"] != "open":
                raise ConflictError("task", message="task is closed")
            if request.operation == "bind":
                if not request.connector_id or not request.context_id:
                    raise ValidationError("connector and context required")
                for field in ("title", "description"):
                    if not task.get(field) and getattr(request, field).strip():
                        task[field] = getattr(request, field).strip()
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

        payload = asdict(request)
        # Keep pre-extension mutation receipts replayable when all new fields are omitted.
        for field, default in (("title", ""), ("description", ""), ("parent_task_id", None),
                               ("query", None), ("top_k", 5)):
            if payload[field] == default:
                payload.pop(field)
        receipt = repo.mutate(
            scope,
            "tasks",
            task_id,
            request.expected_revision,
            request.request_id,
            payload,
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
        visible.setdefault("title", "")
        visible.setdefault("description", "")
        visible.setdefault("parent_task_id", None)
        visible.setdefault("root_task_id", task["task_id"])
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
        request_fingerprint: str = "",
    ) -> dict:
        if self.derivation_extractor is not None and query_output is None:
            raise ValidationError("derivation requires exact read-after-write metadata lookup")
        repo = self.repository
        key = self.event_key(connector, event.binding_id, event.event_id)
        envelope = asdict(event)
        # 幂等摘要覆盖整个请求：只对事件摘要会让「改 event_time／metadata 后重试」被当成
        # 同一事件而静默返回旧记录。指纹写进 envelope 本身，使摘要可从落盘记录复算
        # （recover 就靠这一点核对回执与正文）。
        if request_fingerprint:
            envelope["request_fingerprint"] = request_fingerprint
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
                            "continuity_binding": event.binding_id,
                            "continuity_event": event.event_id,
                            "continuity_operation_id": key,
                            "continuity_connector": connector,
                            "continuity_source_locator": event.source_locator or "",
                            "continuity_claimed_author": event.claimed_author or "",
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
                self._derive_event(scope, key, task, receipt, read, write, query_output)
            if receipt["status"] == "committed" and self.associator is not None:
                if receipt.get("association_status") != "committed":
                    self._associate_event(scope, key, task, receipt, read, write)
            for item in receipt.get("derived", []):
                read(MemoryRef.parse(item["ref"]))
            return {
                **{
                    k: v
                    for k, v in receipt.items()
                    if k not in {"relation_candidates", "build_plan"}
                },
                "index_status": "unchecked",
                "operation_id": key,
            }

    def record_batch(
        self,
        scope,
        events,
        connectors,
        read,
        write_batch,
        *,
        request_fingerprints=None,
        query_output=None,
    ) -> list[dict]:
        """Five-phase reliable batch write (D5).

        1. validate every event against its task/binding and read its evidence refs;
        2. write all missing bodies in **one** ``write_batch`` call;
        3. append background to each affected task **once** (deduplicated by value);
        4. bump each affected task revision **once**;
        5. persist one receipt per event (then run per-event derivation).

        ``write_batch(items)`` takes a list of :class:`ContinuityBatchWrite` (each carries
        its event ``operation_id`` and a ``derived`` flag; derived outputs point at their
        source event) and returns one unit list per item. A batch write failure leaves
        every pending event ``unknown`` so a
        retry reconciles instead of rewriting bodies.
        """
        if self.derivation_extractor is not None and query_output is None:
            raise ValidationError("derivation requires exact read-after-write metadata lookup")
        repo = self.repository
        fingerprints = list(request_fingerprints or [""] * len(events))
        results: list[dict] = []
        pending: list[dict] = []
        resumable: list[dict] = []
        with repo.lock:
            # Phase 1: validate + classify.
            for index, event in enumerate(events):
                connector = connectors[index]
                key = self.event_key(connector, event.binding_id, event.event_id)
                envelope = asdict(event)
                if fingerprints[index]:
                    envelope["request_fingerprint"] = fingerprints[index]
                digest = fingerprint(envelope)
                task = repo.get(scope, "tasks", event.task_id)
                binding = self.binding(task, event.binding_id)
                if task["state"] != "open" or binding["connector_id"] != connector:
                    raise ConflictError(
                        "binding", message="inactive task or mismatched connection"
                    )
                for ref in event.evidence_refs:
                    read(ref)
                try:
                    receipt = repo.get(scope, "events", key)
                except NotFoundError:
                    receipt = None
                if receipt is not None and receipt["digest"] != digest:
                    raise ConflictError("event", message="event ID reused with different content")
                entry = {
                    "index": index,
                    "event": event,
                    "connector": connector,
                    "key": key,
                    "envelope": envelope,
                    "digest": digest,
                    "task": task,
                    "receipt": receipt,
                }
                if receipt is not None:
                    # A receipt that already exists means the body was attempted before.
                    # Re-writing it would duplicate content: a receipt without refs stays
                    # ``unknown`` and is reconciled through ``recover(kind=event)``; one
                    # with refs only needs the promotion (background/revision/committed).
                    for ref in receipt["refs"]:
                        read(MemoryRef.parse(ref))
                    if receipt["refs"]:
                        resumable.append(entry)
                    else:
                        results.append((index, self._receipt_result(receipt, key)))
                else:
                    pending.append(entry)
                entry["results"] = results
            # Phase 2a: register every pending event as ``unknown`` **before** writing a
            # body. Without this, a crash between the body write and the receipt leaves no
            # trace: the retry would write the body again and recovery would have nothing
            # to reconcile against (S11 / D5 order, same as single ``record``).
            for entry in pending:
                receipt = entry["receipt"] or {}
                receipt.update(
                    digest=entry["digest"],
                    status="unknown",
                    refs=[],
                    connector=entry["connector"],
                    task_id=entry["event"].task_id,
                )
                receipt.setdefault("derived", [])
                entry["receipt"] = receipt
                repo.save(scope, "events", entry["key"], receipt)
            # Phase 2b: one batched body write for everything still missing.
            written: dict[int, list] = {}
            if pending:
                try:
                    batches = write_batch(
                        [
                            self._batch_item(
                                entry["key"],
                                entry["event"].content,
                                self._event_metadata(
                                    entry["event"],
                                    entry["connector"],
                                    entry["key"],
                                    entry["envelope"],
                                ),
                                derived=False,
                            )
                            for entry in pending
                        ]
                    )
                    for entry, units in zip(pending, batches):
                        written[entry["index"]] = list(units)
                except Exception:
                    # The bodies may or may not have landed; the durable ``unknown``
                    # receipts already saved above are what recovery reconciles against.
                    for entry in pending:
                        entry["results"].append(
                            (
                                entry["index"],
                                self._receipt_result(entry["receipt"], entry["key"]),
                            )
                        )
                    pending = []
            # Phase 2c: convert the receipts that have bodies into ``partial``.
            for entry in pending:
                units = written.get(entry["index"])
                if units is None:
                    # No body for this item: keep it ``unknown`` instead of pretending it
                    # was written with empty refs.
                    entry["results"].append(
                        (
                            entry["index"],
                            self._receipt_result(entry["receipt"], entry["key"]),
                        )
                    )
                    entry["skip"] = True
                    continue
                entry["receipt"].update(
                    status="partial",
                    refs=[asdict(MemoryRef.of(unit)) for unit in units],
                )
                repo.save(scope, "events", entry["key"], entry["receipt"])
            pending = [entry for entry in pending if not entry.get("skip")]
            # Receipts that already carry refs only need the promotion path below.
            pending = pending + resumable
            # Phase 3 + 4: background once and revision once per task.
            touched: dict[str, dict] = {}
            changed_tasks: set[str] = set()
            for entry in pending:
                units = written.get(entry["index"]) or []
                receipt = entry["receipt"] or {
                    "connector": entry["connector"],
                    "task_id": entry["event"].task_id,
                    "derived": [],
                }
                receipt.update(
                    digest=entry["digest"],
                    refs=[asdict(MemoryRef.of(unit)) for unit in units] or receipt.get("refs", []),
                )
                entry["receipt"] = receipt
                if receipt["refs"]:
                    task_id = entry["event"].task_id
                    task = touched.get(task_id)
                    if task is None:
                        task = repo.get(scope, "tasks", task_id)
                        touched[task_id] = task
                    for ref in receipt["refs"]:
                        item = {"ref": ref, "required": False}
                        if item not in task["background"]:
                            task["background"].append(item)
                            changed_tasks.add(task_id)
            for task_id in changed_tasks:
                task = touched.get(task_id)
                if task is None:
                    continue
                task["revision"] += 1
                repo.save(scope, "tasks", task_id, task)
            # Phase 5: receipts (committed only when refs exist).
            for entry in pending:
                receipt = entry["receipt"]
                receipt["status"] = "committed" if receipt["refs"] else "unknown"
                receipt.setdefault("index_status", "unchecked")
                repo.save(scope, "events", entry["key"], receipt)
                entry["results"].append(
                    (entry["index"], self._receipt_result(receipt, entry["key"]))
                )
            for entry in pending:
                receipt = entry["receipt"]
                if receipt["status"] != "committed" or self.derivation_extractor is None:
                    continue
                task = repo.get(scope, "tasks", entry["event"].task_id)
                self._derive_event(
                    scope,
                    entry["key"],
                    task,
                    receipt,
                    read,
                    self._derived_writer(write_batch, entry["key"]),
                    query_output,
                )
        ordered = dict(results)
        return [ordered[index] for index in range(len(events))]

    @staticmethod
    def _event_metadata(event, connector, key, envelope) -> dict:
        return {
            "continuity_kind": event.kind,
            "continuity_classification_confidence": event.classification_confidence,
            "continuity_task": event.task_id,
            "continuity_binding": event.binding_id,
            "continuity_event": event.event_id,
            "continuity_operation_id": key,
            "continuity_connector": connector,
            "continuity_source_locator": event.source_locator or "",
            "continuity_claimed_author": event.claimed_author or "",
            "continuity_duties": event.duty_ids,
            "continuity_envelope": canonical(envelope),
        }

    @staticmethod
    def _receipt_result(receipt, key) -> dict:
        return {
            **{
                name: value
                for name, value in receipt.items()
                if name not in {"relation_candidates", "build_plan"}
            },
            "index_status": "unchecked",
            "operation_id": key,
        }

    def _derive_event(self, scope, key, task, receipt, read, write, query_output):
        """Journal a side-effect-free extraction plan before applying its outputs.

        Called under repository.lock: concurrent retries of one output cannot both pass
        the lookup/create boundary. Content is written only via the authorized callback.
        The built-in planner reuses exact, authorized snapshots; a new builder/model
        version re-plans created/reused against the same sources without duplicating them.
        """

        from .derivation import DerivationItem, DerivationPlan, reconcile

        repo = self.repository
        try:
            sources = [read(MemoryRef.parse(ref)) for ref in receipt["refs"]]
            build_id = fingerprint({"event": key, "version": self.derivation_version})
            if "build_plan" not in receipt or receipt.get("build_id") != build_id:
                # A derivation_version change is an explicit new-version rebuild: re-plan
                # created/reused against the same sources instead of duplicating them under
                # a fresh build. The batch id and the frozen plan are saved together only
                # after extraction succeeds, so a failed model run never leaves the new id
                # pointing at a stale plan.
                extracted = self.derivation_extractor.extract(sources)
                plan = []
                seen = set()
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
                            try:
                                authorized = read(ref)
                            except (PermissionDeniedError, NotFoundError, ConflictError):
                                continue
                            if MemoryRef.of(authorized).content_digest == digest:
                                item = replace(item, action="reused", ref=ref)
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
                    other = self.resolve_edge(scope, edge.id, read)
                except AgentMemoryError as exc:
                    logger.warning(
                        "Relation support unavailable: error_type=%s", type(exc).__name__
                    )
                    continue
                if (
                    other.relation_type == "supports"
                    and other.status == RelationStatus.CONFIRMED
                ):
                    # Relation truth lives in the Registry KV head; the proof reference
                    # carries the relation id and its payload digest so a later revision
                    # or revocation invalidates the resolution.
                    out.append(
                        (other, MemoryRef(scope, other.relation_id, fingerprint(asdict(other))))
                    )
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
        read_previous: ReadMemory | None = None,
        connector: str = "",
    ) -> dict:
        self._require_role_mode()
        repo = self.repository
        if relation.revision != expected_revision + 1:
            raise ValidationError("relation revision must follow expected_revision")
        source = target = None
        if relation.status != RelationStatus.REVOKED:
            source, target = read(relation.source), read(relation.target)
            self.catalog.validate(relation, source, target)
            # Resolution proofs reference relation heads, not memory units; they are
            # re-checked through validate_resolution, never read as content.
            for ref in relation.evidence_refs:
                read(ref)
        elif expected_revision == 0:
            raise ValidationError("cannot revoke an unknown relation")
        payload = asdict(relation)
        digest = fingerprint(payload)
        with repo.lock:
            try:
                head = repo.get(scope, "relations", relation.relation_id)
            except NotFoundError:
                head = {"revision": 0}
            if head.get("digest") == digest:
                return self._project(scope, relation, head, read)
            if head.get("revision") != expected_revision:
                raise ConflictError("relation", message="stale relation revision")
            current = head.get("current")
            if current:
                previous = RelationRecord.parse(current)
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
                # Immutable prior revision before the head is atomically replaced.
                repo.save(
                    scope,
                    "relations",
                    f"{relation.relation_id}/rev/{previous.revision}",
                    asdict(previous),
                )
            resolved = self._resolve_conflict(scope, relation, source, target, read)
            current = asdict(resolved)
            head.update(
                revision=relation.revision,
                digest=digest,
                status="projecting",
                connector=connector,
                payload_digest=fingerprint(current),
                current=current,
            )
            repo.save(scope, "relations", relation.relation_id, head)
            return self._project(scope, resolved, head, read)

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
                    other = self.resolve_edge(scope, edge.id, read)
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
                    proof_refs.append(
                        MemoryRef(scope, other.relation_id, fingerprint(asdict(other)))
                    )
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
        self._require_role_mode()
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
        heads = [
            h for h in self.repository.list(scope, "relations") if _is_relation_head(h)
        ]
        if not heads:
            return "unknown"
        projected = sum(1 for h in heads if h.get("status") == "committed")
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
            if self.relation_projector is None:
                raise UnsupportedCapabilityError(
                    capability="relation_projection",
                    value=relation.relation_id,
                    component="continuity",
                    message="no relation projector is assembled",
                )
            self.relation_projector.project(scope, relation, relation.revision)
            head["status"] = "committed"
        except Exception:
            head["status"] = "projecting"
        self.repository.save(scope, "relations", relation.relation_id, head)
        return {
            "status": head["status"],
            "relation_id": relation.relation_id,
            "revision": relation.revision,
        }

    def resolve_edge(self, scope: Scope, relation_id: str, read: ReadMemory) -> RelationRecord:
        relation_id = relation_id.removeprefix("continuity:relation:")
        head = self.repository.get(scope, "relations", relation_id)
        if "current" not in head:
            raise NotFoundError("relation")
        self._require_connector_available(scope, head)
        relation = RelationRecord.parse(head["current"])
        if relation.relation_id != relation_id or relation.revision != head.get("revision"):
            raise ConflictError("relation", message="relation head changed")
        self.validate_resolution(scope, relation, read)
        return relation

    def task_background(self, scope, task_id, read):
        gaps = []
        try:
            task = self.repository.get(scope, "tasks", task_id)
        except NotFoundError:
            return {
                "units": [],
                "required_ids": [],
                "gaps": [
                    {
                        "code": "task_not_found",
                        "message": f"continuity task {task_id} is not registered",
                        "blocking": False,
                        "affected_item_ids": [],
                    }
                ],
            }
        units, required_ids = [], []
        for item in task.get("background", []):
            required = bool(item.get("required", True))
            try:
                ref = MemoryRef.parse(item["ref"])
            except (ValueError, KeyError, TypeError):
                gaps.append(
                    {
                        "code": "background_unreadable",
                        "message": "task background reference is not a valid MemoryRef",
                        "blocking": required,
                        "affected_item_ids": [],
                    }
                )
                continue
            try:
                unit = read(ref)
            except AgentMemoryError:
                # 不披露 unit_id：读不到这条背景往往正是因为没有权限，缺口里回显标识
                # 等于把受限记录的 ID 送给无权限调用方（S11 不变量 7）。
                gaps.append(
                    {
                        "code": "background_unavailable",
                        "message": "a task background record is not readable",
                        "blocking": required,
                        "affected_item_ids": [],
                    }
                )
                continue
            units.append(unit)
            if required:
                required_ids.append(unit.id)
        return {"units": units, "required_ids": required_ids, "gaps": gaps}

    def task_owner(self, scope, task_id):
        return self.repository.get(scope, "tasks", task_id)["owner"]

    def retains(self, unit) -> bool:
        """Check all project registries so a cross-scope reference retains its real source."""
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
                    if "current" not in head:
                        continue
                    relation = RelationRecord.parse(head["current"])
                    if relation.status == RelationStatus.REVOKED:
                        continue
                    related = [relation.source, relation.target, *relation.evidence_refs]
                    for ref in related:
                        if ref.key == key:
                            return True
        return False

    def is_referenced(self, scope: Scope, ref: MemoryRef, read: ReadMemory | None = None) -> bool:
        """Whether ``ref`` is still referenced by an active task background or a relation.

        Retention guard for the lifecycle sweep: evidence still cited by an open task's
        background, or by any relation's endpoints/evidence/resolution, must not be cleaned
        up early. Relation truth lives in the Registry KV head, so no ``read`` is required.
        """
        for task in self.repository.list(scope, "tasks"):
            if task.get("state") != "open":
                continue
            if any(item.get("ref") == asdict(ref) for item in task.get("background", [])):
                return True
        for head in self.repository.list(scope, "relations"):
            if not _is_relation_head(head) or "current" not in head:
                continue
            try:
                relation = RelationRecord.parse(head["current"])
            except (ValidationError, ValueError, KeyError, TypeError) as exc:
                # An unreadable relation truth must retain conservatively; only an
                # unexpected programming error may propagate to the caller.
                logger.warning(
                    "Relation truth unreadable during retention check: error_type=%s",
                    type(exc).__name__,
                )
                return True
            if ref in [
                relation.source,
                relation.target,
                *relation.evidence_refs,
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

    def binding_connector(self, scope, task_id, binding_id):
        return self.binding(self.repository.get(scope, "tasks", task_id), binding_id)[
            "connector_id"
        ]

    def continuity_view_result(self, scope, request, read, *, extra_items, top_k: int) -> dict:
        items = []
        gaps = []
        required_ids: set[str] = set()
        seen: set[str] = set()
        if request.anchor is not None:
            try:
                anchor = read(request.anchor)
            except AgentMemoryError:
                # 与角色模式一致：读不到的 anchor 必须报缺口，不能与「没有 anchor」混同。
                gaps.append(
                    {
                        "code": "reference_unavailable",
                        "message": "the anchor reference is not readable",
                        "blocking": False,
                        "affected_item_ids": [],
                    }
                )
                anchor = None
            if anchor is not None:
                seen.add(anchor.id)
                items.append(_unit_entry(anchor, "anchor", required=True))
        if request.task_id:
            background = self.task_background(scope, request.task_id, read)
            required_ids = set(background.get("required_ids") or [])
            gaps.extend(background.get("gaps") or [])
            registered = background.get("units") or []
            ordered = [unit for unit in registered if unit.id in required_ids]
            ordered.extend(unit for unit in reversed(registered) if unit.id not in required_ids)
            for unit in ordered:
                if unit.id in seen:
                    continue
                seen.add(unit.id)
                items.append(
                    _unit_entry(unit, "background", required=unit.id in required_ids)
                )
        for entry in extra_items:
            if entry["unit_id"] in seen:
                continue
            seen.add(entry["unit_id"])
            # 语义召回只给出 unit_id/正文/系统元数据；kind 与 status 按同一映射推导，
            # 它不是已读取的引用，evidence_refs 留空而不是伪造摘要。
            event_kind = str(
                (entry.get("system_metadata") or {}).get("continuity_kind", "statement")
            )
            items.append(
                _view_entry(
                    unit_id=entry["unit_id"],
                    text=entry.get("content", ""),
                    kind=view_kind(event_kind),
                    status=view_status(event_kind),
                    discovery="semantic_candidate",
                    common=True,
                )
            )
        items = _finalize_view(items, top_k=top_k, gaps=gaps)
        duties = request.duty if isinstance(request.duty, list) else [request.duty]
        if any(duty != "general" for duty in duties):
            # 只对「要求了非 general 职责」的查询声明关系展开不可用。
            gaps.append(
                {
                    "code": "relation_expansion_disabled",
                    "message": "relation expansion is not available in continuity mode",
                    "blocking": False,
                    "affected_item_ids": [],
                }
            )
        return {
            "items": items,
            "paths": [],
            "gaps": gaps,
            "meta": {
                "mode": "continuity",
                "duty": request.duty,
                "anchors": [asdict(request.anchor)] if request.anchor is not None else [],
            },
        }

    def view_result(self, scope, request, read, discover, *, top_k: int) -> dict:
        view = self.view(scope, request, read, discover)
        # `anchors` 是 MemoryRef 字典（无 id/text），不是条目：作为结果输出会产生
        # unit_id 为空、正文为空的条目并占掉 top_k 名额，它属于视图元信息。
        items = []
        for group in ("facts", "judgments", "experiences", "open_issues", "risks"):
            for entry in view.get(group) or []:
                refs = entry.get("evidence_refs") or []
                unit_id = (refs[0].get("unit_id") if refs else "") or entry.get("id", "")
                if not unit_id:
                    continue
                # 构建器已算好 kind/status/discovery 等 S11 标注，原样保留；只丢掉条目级
                # 的 id/text（text 已是条目正文，不在标注里重复一份）。
                annotation = {k: v for k, v in entry.items() if k not in {"id", "text"}}
                items.append(
                    _view_entry(unit_id=unit_id, text=entry.get("text", ""), **annotation)
                )
        gaps = list(view.get("gaps") or [])
        items = _finalize_view(items, top_k=top_k, gaps=gaps)
        meta = dict(view.get("meta") or {})
        meta["mode"] = "role"
        meta["schema_version"] = str(view.get("schema_version", ""))
        meta["anchors"] = list(view.get("anchors") or [])
        return {
            "items": items,
            "paths": list(view.get("paths") or []),
            "gaps": gaps,
            "meta": meta,
        }

    def relation_current(self, scope, relation_id):
        return self.repository.get(scope, "relations", relation_id).get("current")

    def _require_connector_available(self, scope, head) -> None:
        """Unavailable submitting connector = unusable relation, reported as not found.

        Fails closed: with no connector configuration injected, a relation that names a
        submitting connector cannot be proven usable and is treated as unavailable.
        """
        connector = head.get("connector") or ""
        if not connector:
            return
        if self.connectors is None:
            raise NotFoundError("relation")
        configured = self.connectors.get(connector)
        if configured is None:
            raise NotFoundError("relation")
        if not configured.get("enabled", True):
            raise NotFoundError("relation")
        if scope.space not in configured.get("projects", []):
            raise NotFoundError("relation")
        if self.source_quarantine(scope, connector) is not None:
            raise NotFoundError("relation")

    def relation_head(self, scope, relation_id) -> dict:
        """The full KV head (including its submitting ``connector``)."""
        return self.repository.get(scope, "relations", relation_id)

    def validate_resolution(self, scope, relation, read):
        """Re-check each supporting relation's current head; revision or revocation invalidates."""
        for proof in relation.resolution_refs:
            try:
                head = self.repository.get(scope, "relations", proof.unit_id)
            except NotFoundError:
                raise ConflictError("resolution", message="resolution evidence changed") from None
            if head.get("payload_digest") != proof.content_digest:
                raise ConflictError("resolution", message="resolution evidence changed")
            supported = RelationRecord.parse(head["current"])
            if (
                supported.status != RelationStatus.CONFIRMED
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
                # created outputs carry this build's identity; reused targets are located
                # by their node key (scope + unit id).
                by_output = {}
                by_reused = {}
                for item in receipt["build_plan"]:
                    if item["action"] == "created":
                        by_output[item["output_key"]] = item
                    elif item["action"] == "reused" and item.get("ref"):
                        by_reused[MemoryRef.parse(item["ref"]).key] = item
                supplied = {}
                for ref in request.refs:
                    unit = read(ref)
                    output = unit.system_metadata.get("continuity_output")
                    if output and unit.system_metadata.get("continuity_build") == build_id:
                        item = by_output.get(output)
                        if item is None:
                            raise ConflictError(
                                "derivation", message="output is not in frozen plan"
                            )
                        if MemoryRef.of(unit).content_digest != item["content_digest"]:
                            raise ConflictError("derivation", message="output digest changed")
                    else:
                        item = by_reused.get(MemoryRef.of(unit).key)
                        if item is None:
                            raise ConflictError(
                                "derivation", message="output is not in frozen plan"
                            )
                        if MemoryRef.of(unit) != MemoryRef.parse(item["ref"]):
                            raise ConflictError("derivation", message="reused reference changed")
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
                    # 改造前 `continuity_event` 存的就是幂等键；读旧数据时回退到它，
                    # 否则历史上停在 unknown 的事件一律核不过。
                    recorded = unit.system_metadata.get(
                        "continuity_operation_id"
                    ) or unit.system_metadata.get("continuity_event")
                    if (
                        recorded != request.operation_id
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
            if request.kind == "relation":
                self._require_role_mode()
                head = self.repository.get(scope, "relations", request.operation_id)
                if "current" not in head:
                    raise NotFoundError("relation")
                relation = RelationRecord.parse(head["current"])
                return self._project(scope, relation, head, read)
            raise ValidationError("unknown recovery kind")

    def repair(self, scope: Scope, read: ReadMemory) -> dict:
        """Rebuild graph projections from authoritative relation heads, not re-extraction."""
        self._require_role_mode()
        results = []
        incomplete = False
        with self.repository.lock:
            for head in self.repository.list(scope, "relations"):
                if not _is_relation_head(head):
                    # Immutable revision rows are not heads: they must not be reported as
                    # "legacy head without current" (that produced a permanent gap).
                    continue
                if "current" not in head:
                    incomplete = True
                    continue
                try:
                    self._require_connector_available(scope, head)
                    relation = RelationRecord.parse(head["current"])
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
        mode = config.get("mode")
        if mode not in {"continuity", "role"}:
            raise ValidationError(
                "continuity.default.params.mode is required and must be 'continuity' or 'role'"
            )
        # Document mode has no precise metadata query; derivation must fail at startup.
        from jiuwen_memory.config.document_flag import should_write_document

        if (
            should_write_document(config.get("write_document", False))
            and config.get("derivation_extractor")
        ):
            raise ValidationError("derivation build is not supported in document mode")

        role_keys = (
            "graph_store", "role_view", "relation_index_builder",
            "relation_types", "relation_types_path",
            "duty_policies", "duty_policies_path",
            "relation_associator", "conflict_resolver",
        )
        if mode == "continuity":
            ignored = [k for k in role_keys if config.get(k)]
            if ignored:
                logger.warning(
                    "continuity mode ignores role-only configuration: %s",
                    ", ".join(sorted(ignored)),
                )
            graph = view = projector = None
            catalog = associator = conflict_resolver = None
        else:
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

            graph = storage.graph(config.get("graph_store", "default"))
            projector = (
                IndexBuilderProducer.dep(config, "relation_index_builder")
                if config.get("relation_index_builder")
                else IndexBuilderProducer.build(
                    "evidence_graph",
                    {"graph_store": config.get("graph_store", "default")},
                    config.ctx,
                )
            )
            catalog = RelationCatalog(
                catalog_data("relation_types", "relation_types_path"),
                catalog_data("duty_policies", "duty_policies_path"),
            )
            associator = (
                AssociatorProducer.build_named(config.get("relation_associator"), config.ctx)
                if config.get("relation_associator")
                else None
            )
            conflict_resolver = (
                ConflictResolverProducer.build_named(config.get("conflict_resolver"), config.ctx)
                if config.get("conflict_resolver")
                else ConflictResolverProducer.build("chain", {}, config.ctx)
            )

        from jiuwen_memory.common.embedder.base import EmbedderProducer
        from jiuwen_memory.common.tokenizer.base import TokenizerProducer
        from jiuwen_memory.construction.dedup import DedupProducer
        from jiuwen_memory.construction.extractor import ExtractorProducer

        connectors = (getattr(config.ctx, "globals", {}) or {}).get("continuity_connectors", {})

        service = ContinuityService(
            repository,
            graph,
            view,
            mode=mode,
            projector=projector,
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
            catalog=catalog,
            associator=associator,
            conflict_resolver=conflict_resolver,
            relation_projector=(
                projector
                if hasattr(projector, "project")
                else IndexBuilderRelationProjector(projector)
            )
            if projector is not None
            else None,
            connectors=connectors,
            task_embedder=EmbedderProducer.build_named(
                config.get("embedder", "default"), config.ctx,
            ) if config.get("embedder", "default") is not None else None,
            task_tokenizer=TokenizerProducer.build_named(
                config.get("tokenizer", "default"), config.ctx,
            ),
        )
        service.health()
        return service
    except BaseException:
        repository.close()
        raise
