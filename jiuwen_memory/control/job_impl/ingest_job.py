# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""In-process background jobs for long-running ingest requests."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import BoundedSemaphore, Event, RLock

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from jiuwen_memory.common.ingest_cancellation import ingest_cancel_signal
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.ingest_job import (
    INGEST_JOB_PREFIX,
    IngestCancellation,
    IngestCancelled,
    IngestJob,
    IngestJobController,
    IngestJobProducer,
    IngestSubmission,
    IngestTask,
    check_ingest_cancelled,
    ingest_cancellation,
)
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.store_manager import StoreManagerProducer, resolve_name

logger = get_logger(__name__)

_JOB_KEY_PREFIX = "/ingest/jobs/"
_PAYLOAD_KEY_PREFIX = "/ingest/payloads/"


@dataclass(frozen=True)
class _PayloadKey:
    org: str
    space: str
    user: str
    agent: str
    session: str
    payload_id: str


class InProcessIngestJobController(IngestJobController):
    """Own queueing, status persistence and payload idempotency for ingest."""

    def __init__(
        self,
        *,
        max_workers: int = 1,
        max_pending_jobs: int = 2,
        kv: KVStore | None = None,
    ) -> None:
        if max_workers <= 0:
            raise ValidationError("max_workers must be greater than zero")
        if max_pending_jobs < 0:
            raise ValidationError("max_pending_jobs must be non-negative")
        self._kv = kv
        self._max_workers = max_workers
        self._executor: ThreadPoolExecutor | None = None
        self._capacity = BoundedSemaphore(max_workers + max_pending_jobs)
        self._jobs: dict[str, IngestJob] = {}
        self._futures: dict[str, Future[None]] = {}
        self._cancellations: dict[str, IngestCancellation] = {}
        self._job_id_by_payload: dict[_PayloadKey, str] = {}
        self._lock = RLock()
        self._closed = False
        self._drained = False

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.INGEST_JOB

    def health(self) -> None:
        return None

    def submit(
        self,
        *,
        payload_id: str,
        source_ref: str,
        scope: Scope,
        task: IngestTask,
    ) -> IngestSubmission:
        key = _payload_key(scope, payload_id)
        now = datetime.now(UTC)
        job = IngestJob(
            id=f"{INGEST_JOB_PREFIX}{uuid.uuid4().hex}",
            payload_id=payload_id,
            source_ref=source_ref,
            scope=scope,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        previous_job_id: str | None = None
        with self._lock:
            if self._closed:
                raise BackendError("ingest job controller is closed")
            existing = self._find_existing(scope, payload_id, key)
            if existing is not None:
                if (
                    existing.status in {"pending", "running", "succeeded"}
                    and existing.source_ref != source_ref
                ):
                    raise ConflictError(
                        "ingest_payload",
                        payload_id,
                        "the same payload_id cannot point to a different source",
                    )
                if existing.status in {"pending", "running", "succeeded"}:
                    return IngestSubmission(existing, reused=True)
                if existing.retry_requires_reconciliation:
                    raise ConflictError(
                        "ingest_payload", payload_id,
                        "previous ingest may have written data; "
                        "reconcile before using a new payload_id",
                    )
                # A failed submission is retryable. Let the caller correct an
                # invalid URI while retaining the same idempotency key.
                previous_job_id = existing.id
            if not self._capacity.acquire(blocking=False):
                raise BackendError("ingest job queue is full")
            self._jobs[job.id] = job
            self._job_id_by_payload[key] = job.id
            try:
                self._persist(job, update_payload=True)
            except Exception:
                self._capacity.release()
                self._jobs.pop(job.id, None)
                if previous_job_id is None:
                    self._job_id_by_payload.pop(key, None)
                else:
                    self._job_id_by_payload[key] = previous_job_id
                raise
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._max_workers,
                    thread_name_prefix="memory-ingest",
                )
            executor = self._executor
            # Publish the future and cancellation signal before a worker can start.
            self._cancellations[job.id] = IngestCancellation(Event())
            try:
                future = executor.submit(self._run, job.id, task)
            except RuntimeError as exc:
                self._cancellations.pop(job.id, None)
                self._capacity.release()
                self._update(job.id, status="failed", error=str(exc))
                raise BackendError(f"failed to submit ingest job: {exc}") from exc
            self._futures[job.id] = future
            future.add_done_callback(lambda completed: self._finished(job.id, completed))
        return IngestSubmission(job, reused=False)

    def _finished(self, job_id: str, future: Future[None]) -> None:
        failure = None if future.cancelled() else future.exception()
        with self._lock:
            if failure is not None:
                current = self._jobs[job_id]
                state = self._cancellations[job_id]
                # Execution is over even when a state-store write failed. Preserve an
                # already computed outcome and its IDs; never leave an orphan running.
                self._jobs[job_id] = replace(
                    current,
                    status="failed" if current.status in {"pending", "running"} else current.status,
                    unit_ids=tuple(dict.fromkeys((*current.unit_ids, *state.unit_ids))),
                    error="; ".join(filter(None, (
                        current.error, f"ingest state persistence failed: {failure}"
                    ))),
                    partial_write=(
                        current.partial_write
                        or (state.write_started and current.status != "succeeded")
                    ),
                    retry_requires_reconciliation=(
                        current.retry_requires_reconciliation
                        or (state.write_started and current.status != "succeeded")
                    ),
                )
            self._futures.pop(job_id, None)
            self._cancellations.pop(job_id, None)
            self._capacity.release()
        if failure is not None:
            logger.error("Ingest worker failed: job_id=%s", job_id,
                         exc_info=(type(failure), failure, failure.__traceback__))

    def status(self, job_id: str, *, scope: Scope) -> IngestJob:
        with self._lock:
            job = self._jobs.get(job_id) or self._load(scope, job_id)
        if job is None or job.scope != scope:
            raise NotFoundError("ingest_job", job_id)
        return job

    def cancel(self, job_id: str, *, scope: Scope) -> None:
        with self._lock:
            job = self.status(job_id, scope=scope)
            if job.status not in {"pending", "running"} or job.cancel_requested:
                return
            state = self._cancellations.get(job_id)
            future = self._futures.get(job_id)
            if state is None or future is None:
                raise BackendError("ingest job has no active execution")
            updated = replace(
                job, cancel_requested=True, updated_at=datetime.now(UTC),
                status="cancelled" if job.status == "pending" else job.status,
            )
            self._persist(updated)
            self._jobs[job_id] = updated
            state.requested.set()
            # If the worker is already waiting for the lock, _run observes cancelled.
            future.cancel()

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._drained:
                return
            self._closed = True
            executor = self._executor
        # 不在状态锁内 join：任务结束时还要取得该锁持久化最终状态。
        # admission 已封闭仍允许后续 wait=True 升级为等待已提交任务全部结束。
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=False)
        if wait or executor is None:
            with self._lock:
                self._drained = True

    def _find_existing(
        self,
        scope: Scope,
        payload_id: str,
        key: _PayloadKey,
    ) -> IngestJob | None:
        job_id = self._job_id_by_payload.get(key)
        existing = self._jobs.get(job_id or "")
        if existing is None and self._kv is not None:
            mapping_key = _payload_storage_key(payload_id)
            if self._kv.exists(scope, mapping_key):
                job_id = self._kv.get(scope, mapping_key).decode("utf-8")
                existing = self._load(scope, job_id)
        if existing is not None:
            if existing.scope != scope:
                return None
            if existing.id not in self._jobs:
                # This is the authorized submit path, unlike the read-only status path.
                self._persist(existing)
            self._jobs[existing.id] = existing
            self._job_id_by_payload[key] = existing.id
        return existing

    def _run(self, job_id: str, task: IngestTask) -> None:
        with self._lock:
            if self._jobs[job_id].status == "cancelled":
                return
            self._update(job_id, status="running")
            state = self._cancellations[job_id]
        token = ingest_cancellation.set(state)
        signal_token = ingest_cancel_signal.set(state.requested)
        status, error = "succeeded", ""
        unit_ids = state.unit_ids
        try:
            check_ingest_cancelled()
            units = task()
            unit_ids = tuple(dict.fromkeys((*state.unit_ids, *(u.id for u in units))))
        except IngestCancelled as exc:
            status, error, unit_ids = "cancelled", str(exc), state.unit_ids
        except Exception as exc:
            logger.exception("Ingest job failed: job_id=%s", job_id)
            status, error, unit_ids = "failed", str(exc), state.unit_ids
        finally:
            ingest_cancel_signal.reset(signal_token)
            ingest_cancellation.reset(token)
        with self._lock:
            if status == "succeeded" and state.requested.is_set():
                status = "cancelled"
            self._update(
                job_id, status=status, error=error, unit_ids=unit_ids,
                partial_write=state.write_started and status != "succeeded",
                retry_requires_reconciliation=state.write_started and status != "succeeded",
            )

    def _update(
        self,
        job_id: str,
        *,
        status: str,
        unit_ids: tuple[str, ...] = (),
        error: str = "",
        partial_write: bool = False,
        retry_requires_reconciliation: bool = False,
    ) -> None:
        with self._lock:
            current = self._jobs[job_id]
            updated = replace(
                current,
                status=status,
                updated_at=datetime.now(UTC),
                unit_ids=unit_ids,
                error=error,
                partial_write=partial_write,
                retry_requires_reconciliation=retry_requires_reconciliation,
            )
            self._jobs[job_id] = updated
            self._persist(updated)

    def _persist(self, job: IngestJob, *, update_payload: bool = False) -> None:
        if self._kv is None:
            return
        value = json.dumps(
            {
                "id": job.id,
                "payload_id": job.payload_id,
                "source_ref": job.source_ref,
                "status": job.status,
                "created_at": job.created_at.isoformat(),
                "updated_at": job.updated_at.isoformat(),
                "unit_ids": list(job.unit_ids),
                "error": job.error,
                "cancel_requested": job.cancel_requested,
                "partial_write": job.partial_write,
                "retry_requires_reconciliation": job.retry_requires_reconciliation,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        job_key = _job_storage_key(job.id)
        if self._kv.exists(job.scope, job_key):
            self._kv.update(job.scope, job_key, value)
        else:
            self._kv.insert(job.scope, job_key, value)
        if not update_payload:
            return
        payload_key = _payload_storage_key(job.payload_id)
        payload_value = job.id.encode("utf-8")
        if self._kv.exists(job.scope, payload_key):
            self._kv.update(job.scope, payload_key, payload_value)
        else:
            self._kv.insert(job.scope, payload_key, payload_value)

    def _load(self, scope: Scope, job_id: str) -> IngestJob | None:
        if self._kv is None or not self._kv.exists(scope, _job_storage_key(job_id)):
            return None
        try:
            data = json.loads(self._kv.get(scope, _job_storage_key(job_id)).decode("utf-8"))
            job = IngestJob(
                id=str(data["id"]),
                payload_id=str(data["payload_id"]),
                source_ref=str(data["source_ref"]),
                scope=scope,
                status=str(data["status"]),
                created_at=datetime.fromisoformat(str(data["created_at"])),
                updated_at=datetime.fromisoformat(str(data["updated_at"])),
                unit_ids=tuple(str(item) for item in data.get("unit_ids", [])),
                error=str(data.get("error", "")),
                cancel_requested=bool(data.get("cancel_requested", False)),
                partial_write=bool(data.get("partial_write", False)),
                retry_requires_reconciliation=bool(
                    data.get("retry_requires_reconciliation", False)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BackendError(f"invalid persisted ingest job {job_id!r}") from exc
        if job.status in {"pending", "running"}:
            job = replace(
                job,
                status="failed",
                updated_at=datetime.now(UTC),
                error="ingest job was interrupted by server restart",
                partial_write=job.status == "running",
                retry_requires_reconciliation=job.status == "running",
            )
            # Recovery here is a read-only projection. Status is called before PEP
            # authorization; only authorized mutation paths may persist recovery.
        return job


def _payload_key(scope: Scope, payload_id: str) -> _PayloadKey:
    return _PayloadKey(
        org=scope.org,
        space=scope.space,
        user=scope.user,
        agent=scope.agent,
        session=scope.session,
        payload_id=payload_id,
    )


def _job_storage_key(job_id: str) -> str:
    return f"{_JOB_KEY_PREFIX}{job_id}"


def _payload_storage_key(payload_id: str) -> str:
    return f"{_PAYLOAD_KEY_PREFIX}{payload_id}"


@IngestJobProducer.register("in_process")
def _build(config):
    return InProcessIngestJobController(
        max_workers=int(config.get("ingest_max_workers", 1)),
        max_pending_jobs=int(config.get("ingest_max_pending_jobs", 2)),
        kv=StoreManagerProducer.resolve(config).kv(resolve_name(config, "kv_store")),
    )
