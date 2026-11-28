# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Recall for the local hooks (F17 D8): project, binding and pending-turn decisions.

Host adapters only translate events; every handoff decision (project resolution,
binding selection, pending-record creation, recall budget) lives here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from jiuwen_memory.api import (
    Context,
    ContinuityRecovery,
    ContinuityWrite,
    MemoryRef,
    PermissionDeniedError,
    Scope,
    TaskRequest,
    ValidationError,
    WriteIncompleteError,
)
from jiuwen_memory_entry.local.capture_filter import build_turn_content
from jiuwen_memory_entry.local.turns import (
    Operation,
    Selection,
    TurnKey,
    TurnStore,
)

RECALL_TIMEOUT_SECONDS = 2.0
TERMINAL_OPERATION_STATUSES = frozenset({"committed", "rejected", "stopped", "expired"})
VIEW_BYTE_LIMIT = 16 * 1024
DEFAULT_TOP_K = 10
_OPERATION_LOCKS: dict[str, threading.Lock] = {}
_OPERATION_LOCKS_GUARD = threading.Lock()


def _operation_lock(local_op_id: str) -> threading.Lock:
    """One lock per operation across every HookService instance in this process."""
    with _OPERATION_LOCKS_GUARD:
        return _OPERATION_LOCKS.setdefault(local_op_id, threading.Lock())


def _forget_operation_lock(local_op_id: str) -> None:
    """Drop an operation's lock after an attempt so a long-lived service does not grow.

    The state is persisted before this runs, so a concurrent caller that creates a fresh
    lock re-reads the record and acts on the latest status.
    """
    with _OPERATION_LOCKS_GUARD:
        _OPERATION_LOCKS.pop(local_op_id, None)


@dataclass(frozen=True)
class InstallPolicy:
    """The caller's installation, resolved from the token and install record."""

    install_id: str
    connector_id: str
    capture: Literal["off", "task"]
    fixed_project: str | None
    projects: tuple[str, ...]
    org: str
    actor: Scope | None = None


@dataclass(frozen=True)
class RecallRequest:
    host_session_id: str
    turn_id: str
    prompt: str
    project: str | None = None
    selection: Selection | None = None
    duty: str | list[str] | None = None
    top_k: int | None = None


@dataclass(frozen=True)
class CaptureRequest:
    host_session_id: str
    turn_id: str
    prompt: str
    answer: str
    project: str | None = None


@dataclass
class CaptureResult:
    status: Literal[
        "committed", "partial", "unknown", "rejected", "conflict", "not_written",
        "unconfirmed",
    ]
    local_op_id: str = ""
    event_id: str = ""
    refs: list[dict] = field(default_factory=list)
    operation_id: str = ""
    reason: str = ""
    next_actions: list[str] = field(default_factory=list)


@dataclass
class RecallResult:
    status: Literal["ok", "need_project", "need_selection", "unbound"]
    view: dict | None = None
    candidates: list[dict] = field(default_factory=list)
    notice: str | None = None


def _jsonable(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {key: _jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {str(key): _jsonable(item) for key, item in vars(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class RecallExecutor:
    """Bounded execution for recall search.

    A timeout ends the wait, not the worker; the semaphore caps how many slow searches
    can be alive at once, and ``close`` drains the current runtime's workers before its
    API is closed.
    """

    def __init__(self, max_workers: int = 4, *, drain_timeout: float = 2.0) -> None:
        self._slots = threading.BoundedSemaphore(max_workers)
        self._active: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._drain_timeout = drain_timeout

    def run(self, function, timeout: float):
        if self._closed or not self._slots.acquire(blocking=False):
            return None
        box: dict = {}

        def target() -> None:
            try:
                box["result"] = function()
            except Exception:  # noqa: BLE001 - recall never propagates to the host
                pass
            finally:
                with self._lock:
                    self._active.discard(thread)
                self._slots.release()

        thread = threading.Thread(target=target, daemon=True)
        with self._lock:
            self._active.add(thread)
        thread.start()
        thread.join(timeout)
        return None if thread.is_alive() else box.get("result")

    def close(self) -> None:
        self._closed = True
        with self._lock:
            active = list(self._active)
        deadline = time.monotonic() + self._drain_timeout
        for thread in active:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)


class HookService:
    def __init__(
        self,
        api,
        turns: TurnStore,
        policy: InstallPolicy,
        *,
        default_top_k: int = DEFAULT_TOP_K,
        timeout: float = RECALL_TIMEOUT_SECONDS,
        executor: RecallExecutor | None = None,
    ) -> None:
        self._api = api
        self._turns = turns
        self._policy = policy
        self._default_top_k = default_top_k
        self._timeout = timeout
        self._executor = executor or RecallExecutor()

    def recall(self, security, request: RecallRequest) -> RecallResult:
        project = self._resolve_project(request)
        if project is None:
            return RecallResult(
                "need_project",
                notice="this install serves several projects; pass one explicitly",
            )
        scope = Scope(org=self._policy.org, space=project)
        tasks = self._find(scope, security, request)
        candidates = [self._candidate(task) for task in tasks]
        matches = [task for task in tasks if task.get("binding_id")]
        if request.selection is not None:
            wanted = (request.selection.task_id, request.selection.binding_id)
            selected = []
            for task in tasks:
                current = (task.get("task_id"), task.get("binding_id"))
                if current == wanted:
                    selected.append(task)
            if not selected:
                raise ValidationError("the selected task is not bound to this session")
            self._turns.set_selection(request.selection)
            matches = selected
        elif len(matches) > 1:
            saved = self._turns.get_selection(
                self._policy.connector_id, project, request.host_session_id
            )
            if saved is not None:
                saved_key = (saved.task_id, saved.binding_id)
                narrowed = []
                for task in matches:
                    current = (task.get("task_id"), task.get("binding_id"))
                    if current == saved_key:
                        narrowed.append(task)
                matches = narrowed or matches

        top_k = request.top_k or self._default_top_k
        if len(matches) == 1:
            task_id = str(matches[0]["task_id"])
            binding_id = str(matches[0]["binding_id"])
            view = self._search(security, scope, request, task_id=task_id, top_k=top_k)
            if self._policy.capture == "task":
                injected = [
                    str(item.get("content", ""))
                    for item in (view or {}).get("items") or []
                    if isinstance(item, dict)
                ]
                self._turns.put_pending(
                    TurnKey(self._policy.connector_id, project, request.host_session_id,
                            request.turn_id),
                    task_id,
                    binding_id,
                    request.prompt,
                    injected=injected,
                )
            return RecallResult("ok", view=view)
        if len(matches) > 1:
            return RecallResult(
                "need_selection",
                candidates=[self._candidate(task) for task in matches],
                notice="this session is bound to several tasks; choose one explicitly",
            )
        view = self._search(security, scope, request, task_id=None, top_k=top_k)
        return RecallResult(
            "unbound",
            view=view,
            candidates=candidates,
            notice="this session is not bound to a task; this turn will not be saved",
        )

    def _resolve_project(self, request: RecallRequest) -> str | None:
        if request.project:
            if request.project not in self._policy.projects:
                raise ValidationError("this install is not connected to that project")
            return request.project
        if self._policy.fixed_project:
            return self._policy.fixed_project
        if len(self._policy.projects) == 1:
            return self._policy.projects[0]
        return None

    def _find(self, scope: Scope, security, request: RecallRequest) -> list[dict]:
        result = self._api.continuity_task(
            scope,
            TaskRequest(
                "find",
                connector_id=self._policy.connector_id,
                context_id=request.host_session_id,
            ),
            security=security,
        )
        tasks = list(result.get("tasks") or [])
        for task in tasks:
            task["binding_id"] = self._active_binding(
                scope, security, task["task_id"], request.host_session_id
            )
        if tasks:
            return tasks
        if request.prompt.strip():
            candidates = self._api.continuity_task(
                scope,
                TaskRequest("find", query=request.prompt),
                security=security,
            )
            return list(candidates.get("tasks") or [])
        return []

    def _active_binding(self, scope: Scope, security, task_id: str, host_session_id: str) -> str:
        """Resolve the session's active binding; ``find`` only reports the task."""
        record = self._api.continuity_task(
            scope, TaskRequest("get", task_id=task_id), security=security
        )
        for binding in record.get("bindings") or []:
            if (
                binding.get("connector_id") == self._policy.connector_id
                and binding.get("context_id") == host_session_id
                and binding.get("active")
            ):
                return str(binding.get("binding_id", ""))
        return ""

    @staticmethod
    def _candidate(task: dict) -> dict:
        return {
            "task_id": task.get("task_id", ""),
            "title": task.get("title", ""),
            "description": task.get("description", ""),
            "state": task.get("state", ""),
            "match_type": task.get("match_type", ""),
        }

    # -- capture -------------------------------------------------------------- #

    def capture(self, security, request: CaptureRequest) -> CaptureResult:
        project = self._resolve_project(
            RecallRequest(
                host_session_id=request.host_session_id,
                turn_id=request.turn_id,
                prompt=request.prompt,
                project=request.project,
            )
        )
        if project is None:
            return CaptureResult("not_written", reason="project undetermined")
        key = TurnKey(
            self._policy.connector_id, project, request.host_session_id, request.turn_id
        )
        pending = self._turns.get_pending(key)
        existing = self._turns.get_operation(key)
        # The host's Stop payload usually has no prompt: use the text recall saved.
        prompt = request.prompt.strip() or (pending.user_text if pending else "")
        content = build_turn_content(
            prompt, request.answer, injected=pending.injected if pending else ()
        )
        # Identity of a retry is the answer being captured, not the pending prompt row
        # (which expires first) and not a reconstruction of the frozen content (which
        # would hide a different answer).
        digest = self._digest(request.answer, project)
        if existing is not None:
            return self._replay(security, existing, digest)
        if self._policy.capture != "task":
            # A pending row may have been created before the user switched capture off;
            # a late Stop must not start a new operation from it. Existing operations
            # were already handled above and keep their query/idempotency semantics.
            return CaptureResult(
                "not_written", reason="capture is off; the pending turn was not written"
            )
        if pending is None:
            return CaptureResult("not_written", reason="no pending turn for this session")
        reason = self._binding_invalid(security, project, pending)
        if reason:
            return CaptureResult("rejected", reason=reason)
        operation = Operation(
            local_op_id="op-" + uuid.uuid4().hex[:16],
            key=key,
            install_id=self._policy.install_id,
            actor=dict(self._actor()),
            task_id=pending.task_id,
            binding_id=pending.binding_id,
            event_id=self._event_id(key),
            digest=digest,
            status="prepared",
            request={
                "content": content,
                "project": project,
                "task_id": pending.task_id,
                "binding_id": pending.binding_id,
                "event_id": self._event_id(key),
                "digest": digest,
            },
            created_at=time.time(),
            request_expires_at=time.time() + 7 * 24 * 60 * 60,
            association_until=time.time() + 30 * 24 * 60 * 60,
        )
        try:
            created = self._turns.create_operation(operation)
        except Exception:
            # The operation record could not be persisted (locked or disk full): nothing
            # was attempted, so this is "unconfirmed", never "not written".
            return CaptureResult(
                "unconfirmed",
                reason="operation record could not be written; nothing was submitted",
                next_actions=["retry this turn later"],
            )
        if created.local_op_id != operation.local_op_id:
            return self._replay(security, created, digest)
        return self._advance(security, created)

    def _actor(self) -> dict:
        actor = self._policy.actor
        return dataclasses.asdict(actor) if actor is not None else {}

    def _digest(self, answer: str, project: str) -> str:
        # Deliberately independent of pending/task/binding and of the user prompt: a
        # Stop retry may no longer carry the prompt after the pending row expires, while
        # the answer is still what must be compared.
        payload = {"answer": answer, "org": self._policy.org, "project": project}
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _event_id(key: TurnKey) -> str:
        raw = "\0".join((key.connector, key.project, key.host_session_id, key.turn_id))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _binding_invalid(self, security, project: str, pending) -> str:
        record = self._api.continuity_task(
            Scope(org=self._policy.org, space=project),
            TaskRequest("get", task_id=pending.task_id),
            security=security,
        )
        if record.get("state") != "open":
            return "the task was closed before this turn was written"
        for binding in record.get("bindings") or []:
            if (
                binding.get("binding_id") == pending.binding_id
                and binding.get("active")
                and binding.get("connector_id") == self._policy.connector_id
            ):
                return ""
        return "the binding was revoked or replaced before this turn was written"

    def _replay(self, security, operation: Operation, digest: str) -> CaptureResult:
        if operation.digest != digest:
            return CaptureResult(
                "conflict",
                local_op_id=operation.local_op_id,
                reason="this turn was frozen with a different answer",
            )
        if operation.status == "committed":
            receipt = operation.receipt or {}
            refs, operation_id = self._echo(
                security, list(receipt.get("refs") or []), str(receipt.get("operation_id", ""))
            )
            return CaptureResult(
                "committed",
                local_op_id=operation.local_op_id,
                event_id=operation.event_id,
                refs=refs,
                operation_id=operation_id,
            )
        if operation.status == "rejected":
            return CaptureResult(
                "rejected",
                local_op_id=operation.local_op_id,
                reason="the frozen write was rejected",
            )
        try:
            return self._advance_with_receipt(security, operation)
        except ValidationError as exc:
            return CaptureResult(
                "unconfirmed",
                local_op_id=operation.local_op_id,
                reason=str(exc),
                next_actions=["inspect the operation with the CLI, then retry"],
            )

    def _advance(
        self, security, operation: Operation, *, consult_receipt: bool = False
    ) -> CaptureResult:
        """Serialize advancement per operation so one turn never writes twice.

        The kernel receipt probe and the record update it drives run under the same lock
        as the write, so a concurrent capture cannot interleave between them.
        """
        lock = _operation_lock(operation.local_op_id)
        with lock:
            current = self._turns.get_operation_by_id(operation.local_op_id) or operation
            if current.status == "committed":
                receipt = current.receipt or {}
                refs, operation_id = self._echo(
                    security,
                    list(receipt.get("refs") or []),
                    str(receipt.get("operation_id", "")),
                )
                result = CaptureResult(
                    "committed",
                    local_op_id=current.local_op_id,
                    refs=refs,
                    operation_id=operation_id,
                )
            else:
                kernel_receipt = self._receipt(current) if consult_receipt else None
                if kernel_receipt is not None:
                    result = self._from_receipt(security, current, kernel_receipt)
                else:
                    result = self._advance_locked(security, current)
        # Release by the persisted record status, not the returned status: "stopped" and
        # "expired" are record states that no CaptureResult carries. Dropping a lock while
        # the operation can still advance would let a third thread race on a fresh lock.
        final = self._turns.get_operation_by_id(operation.local_op_id)
        if final is None or final.status in TERMINAL_OPERATION_STATUSES:
            _forget_operation_lock(operation.local_op_id)
        return result

    def _advance_locked(self, security, operation: Operation) -> CaptureResult:
        if not operation.request:
            return self._nonterminal(operation, "the original write request was discarded")
        frozen = operation.request
        scope = Scope(org=self._policy.org, space=frozen["project"])
        try:
            units = self._api.add(
                frozen["content"],
                scope,
                security=security,
                continuity=ContinuityWrite(
                    frozen["task_id"], frozen["binding_id"], frozen["event_id"]
                ),
            )
        except WriteIncompleteError as exc:
            receipt = {
                "status": exc.status,
                "operation_id": exc.operation_id,
                "retry_action": exc.retry_action,
                "refs": [_jsonable(ref) for ref in exc.refs],
            }
            self._turns.update_operation(
                operation.local_op_id, status=exc.status, receipt=receipt
            )
            return CaptureResult(
                exc.status,
                local_op_id=operation.local_op_id,
                refs=receipt["refs"],
                operation_id=exc.operation_id,
                reason="write is not confirmed; retry this turn to finish it",
            )
        except PermissionDeniedError:
            # Permission loss is a state transition, not a transport failure: let the
            # caller stop the operation instead of retrying forever.
            raise
        except Exception as exc:  # noqa: BLE001 - host must not be interrupted
            self._turns.update_operation(operation.local_op_id, status="unknown")
            return self._nonterminal(operation, f"write failed: {type(exc).__name__}")
        refs = [_jsonable(MemoryRef.of(unit)) for unit in units]
        kernel_operation_id = self._kernel_operation_id(operation)
        receipt = {
            "status": "committed",
            "operation_id": kernel_operation_id,
            "event_id": operation.event_id,
            "refs": refs,
        }
        self._turns.update_operation(
            operation.local_op_id, status="committed", receipt=receipt, request=None
        )
        visible, operation_id = self._echo(security, refs, kernel_operation_id)
        return CaptureResult(
            "committed",
            local_op_id=operation.local_op_id,
            event_id=operation.event_id,
            refs=visible,
            operation_id=operation_id,
        )

    def _kernel_operation_id(self, operation: Operation) -> str:
        project = str((operation.request or {}).get("project", ""))
        try:
            receipt = self._api.continuity_receipt_status(
                Scope(org=self._policy.org, space=project),
                operation.task_id,
                operation.binding_id,
                operation.event_id,
            )
        except Exception:  # noqa: BLE001 - a missing probe must not fail the write
            return ""
        return str(getattr(receipt, "operation_id", "")) if receipt else ""

    def retry(self, security, local_op_id: str) -> CaptureResult:
        """Advance a frozen operation without recomputing its digest (service recovery).

        The kernel receipt is consulted first: after a permission change a prepared write
        may already have executed, so retrying blindly could write twice. The probe is
        read-only and the local service is the trusted caller (D8).
        """
        operation = self._turns.get_operation_by_id(local_op_id)
        if operation is None:
            raise ValidationError("unknown operation")
        if operation.install_id != self._policy.install_id:
            raise ValidationError("the operation belongs to another install")
        if operation.status == "rejected":
            return CaptureResult(
                "rejected", local_op_id=local_op_id, reason="the frozen write was rejected"
            )
        return self._advance_with_receipt(security, operation)

    def _advance_with_receipt(self, security, operation: Operation) -> CaptureResult:
        return self._advance(security, operation, consult_receipt=True)

    def _receipt(self, operation: Operation):
        """Read what the kernel already did; ``None`` means "no record, safe to write"."""
        if not operation.request:
            return None
        probe = getattr(self._api, "continuity_receipt_status", None)
        if probe is None:
            return None
        scope = Scope(org=self._policy.org, space=str(operation.request.get("project", "")))
        try:
            return probe(scope, operation.task_id, operation.binding_id, operation.event_id)
        except Exception as exc:  # noqa: BLE001 - fail closed: never write after a failed probe
            raise ValidationError(
                f"kernel receipt lookup failed ({type(exc).__name__}); write not attempted"
            ) from exc

    def _from_receipt(self, security, operation: Operation, receipt) -> CaptureResult:
        status = str(getattr(receipt, "status", "unknown"))
        refs = [
            dict(item) if isinstance(item, dict) else item
            for item in (getattr(receipt, "refs", None) or [])
        ]
        operation_id = str(getattr(receipt, "operation_id", ""))
        retry_action = str(getattr(receipt, "retry_action", ""))
        if status == "committed":
            self._turns.update_operation(
                operation.local_op_id,
                status="committed",
                receipt={"status": "committed", "operation_id": operation_id,
                         "event_id": operation.event_id, "refs": refs},
                request=None,
            )
            _forget_operation_lock(operation.local_op_id)
            visible, echoed = self._echo(security, refs, operation_id)
            return CaptureResult(
                "committed",
                local_op_id=operation.local_op_id,
                refs=visible,
                operation_id=echoed,
            )
        self._turns.update_operation(
            operation.local_op_id,
            status=status,
            receipt={
                "status": status,
                "operation_id": operation_id,
                "refs": refs,
                "retry_action": retry_action,
            },
        )
        if retry_action and status in {"partial", "unknown"}:
            return self._retry_from_receipt(
                security, operation, refs, operation_id, retry_action
            )
        if status in TERMINAL_OPERATION_STATUSES:
            _forget_operation_lock(operation.local_op_id)
        # The caller may no longer be able to read what was written: echo only what the
        # current permissions allow.
        visible, echoed = self._echo(security, refs, operation_id)
        return CaptureResult(
            status,  # type: ignore[arg-type]
            local_op_id=operation.local_op_id,
            refs=visible,
            operation_id=echoed,
            reason="kernel receipt is not terminal; no retry action is available",
        )

    def _echo(
        self, security, refs: list[dict], operation_id: str
    ) -> tuple[list[dict], str]:
        """Echo refs only while the caller can still read every written unit (D8)."""
        for ref in refs:
            scope = ref.get("scope") or {}
            try:
                self._api.get(
                    str(ref.get("unit_id", "")), Scope(**scope), security=security
                )
            except Exception:  # noqa: BLE001 - unauthorized must not break the turn
                return [], ""
        return refs, operation_id

    def _retry_from_receipt(
        self,
        security,
        operation: Operation,
        refs: list[dict],
        operation_id: str,
        retry_action: str,
    ) -> CaptureResult:
        """Follow the kernel's retry_action; permission loss stops instead of looping."""
        scope = Scope(
            org=self._policy.org,
            space=str((operation.request or {}).get("project", "")),
        )
        try:
            if retry_action == "recover_then_retry" and operation_id:
                memory_refs = []
                for item in refs:
                    if not isinstance(item, dict):
                        continue
                    if not (item.get("unit_id") and item.get("content_digest")):
                        continue
                    memory_refs.append(MemoryRef.parse(item))
                if memory_refs:
                    self._api.continuity_recover(
                        scope,
                        security=security,
                        recovery=ContinuityRecovery(
                            kind="event", operation_id=operation_id, refs=memory_refs
                        ),
                    )
            # Already inside _advance's per-operation lock; re-acquiring would deadlock.
            return self._advance_locked(security, operation)
        except (PermissionDeniedError, ValidationError) as exc:
            self._turns.update_operation(
                operation.local_op_id,
                status="stopped",
                receipt={
                    "status": "stopped",
                    "operation_id": operation_id,
                    "refs": refs,
                    "retry_action": retry_action,
                    "reason": type(exc).__name__,
                },
            )
            _forget_operation_lock(operation.local_op_id)
            return CaptureResult(
                "stopped",
                local_op_id=operation.local_op_id,
                refs=refs,
                operation_id=operation_id,
                reason="write permission is no longer valid; only receipt checks remain",
            )

    @staticmethod
    def _nonterminal(operation: Operation, reason: str) -> CaptureResult:
        if time.time() > operation.association_until:
            return CaptureResult(
                "unconfirmed",
                local_op_id=operation.local_op_id,
                reason="the turn association expired; only status can be queried",
            )
        return CaptureResult(
            "unconfirmed",
            local_op_id=operation.local_op_id,
            reason=reason,
            next_actions=["recover with the original operation id, then retry the turn"],
        )

    def _search(self, security, scope: Scope, request: RecallRequest, *, task_id, top_k):
        kwargs = {}
        if task_id:
            kwargs["task_id"] = task_id
        if request.duty:
            kwargs["duty"] = request.duty
        # A context manager would join the worker on exit and defeat the timeout; the
        # bounded executor caps slow backends and is drained with the runtime.
        result = self._executor.run(
            lambda: self._api.search(
                request.prompt,
                Context(scope),
                security=security,
                top_k=top_k,
                **kwargs,
            ),
            self._timeout,
        )
        if result is None:
            return None
        return _trim_view(_jsonable(result))


def _trim_view(view: dict) -> dict:
    encoded = json.dumps(view, ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= VIEW_BYTE_LIMIT:
        return view
    items = view.get("items")
    if isinstance(items, list):
        while items and len(json.dumps(view, ensure_ascii=False).encode("utf-8")) > VIEW_BYTE_LIMIT:
            items.pop()
        gaps = view.setdefault("gaps", [])
        if isinstance(gaps, list):
            gaps.append(
                {"code": "budget_exhausted", "message": "recall byte budget exhausted",
                 "blocking": False}
            )
    return view
