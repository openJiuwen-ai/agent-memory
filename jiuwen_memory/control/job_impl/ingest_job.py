# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""In-process background jobs for long-running ingest requests."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import BoundedSemaphore, RLock

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.ingest_job import (
    INGEST_JOB_PREFIX,
    IngestJob,
    IngestJobController,
    IngestJobProducer,
    IngestSubmission,
    IngestTask,
)
from jiuwen_memory.control.job_impl.job_state import InMemoryJobStateStore
from jiuwen_memory.control.job_state import JobStateStore, JobStateStoreProducer

logger = get_logger(__name__)


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
        state_store: JobStateStore | None = None,
    ) -> None:
        if max_workers <= 0:
            raise ValidationError("max_workers must be greater than zero")
        if max_pending_jobs < 0:
            raise ValidationError("max_pending_jobs must be non-negative")
        self._state_store = state_store or InMemoryJobStateStore()
        self._max_workers = max_workers
        self._executor: ThreadPoolExecutor | None = None
        self._capacity = BoundedSemaphore(max_workers + max_pending_jobs)
        self._jobs: dict[str, IngestJob] = {}
        self._job_id_by_payload: dict[_PayloadKey, str] = {}
        self._lock = RLock()
        self._closed = False

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.INGEST_JOB

    def health(self) -> None:
        self._state_store.health()

    def submit(
        self,
        *,
        payload_id: str,
        source_ref: str,
        scope: Scope,
        task: IngestTask,
        owner: Scope | None = None,
    ) -> IngestSubmission:
        key = _payload_key(scope, payload_id)
        now = datetime.now(timezone.utc)
        job = IngestJob(
            id=f"{INGEST_JOB_PREFIX}{uuid.uuid4().hex}",
            payload_id=payload_id,
            source_ref=source_ref,
            scope=scope,
            owner=owner,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        previous_job_id: str | None = None
        with self._lock:
            if self._closed:
                raise BackendError("ingest job controller is closed")
            existing = self._find_existing(scope, payload_id, key, owner=owner)
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
                # A failed submission is retryable. Let the caller correct an
                # invalid URI while retaining the same idempotency key.
                previous_job_id = existing.id
            if not self._capacity.acquire(blocking=False):
                raise BackendError("ingest job queue is full")
            self._jobs[job.id] = job
            self._job_id_by_payload[key] = job.id
            try:
                self._persist(job)
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
        try:
            future = executor.submit(self._run, job.id, task)
            future.add_done_callback(lambda _future: self._capacity.release())
        except RuntimeError as exc:
            self._capacity.release()
            self._update(job.id, status="failed", error=str(exc))
            raise BackendError(f"failed to submit ingest job: {exc}") from exc
        return IngestSubmission(job, reused=False)

    def status(
        self,
        job_id: str,
        *,
        scope: Scope,
        owner: Scope | None = None,
    ) -> IngestJob:
        with self._lock:
            persisted = self._state_store.get(job_id, scope=scope, owner=owner)
            if persisted is None:
                job = None
            else:
                if job_id in self._jobs:
                    # The durable store is authoritative once this process has
                    # already loaded the job; refresh the in-process snapshot.
                    job = persisted
                    self._jobs[job_id] = persisted
                else:
                    # Only a cold read applies restart interruption semantics.
                    job = self._load(scope, job_id, owner=owner, persisted=persisted)
        owner_mismatch = (
            owner is not None and job is not None and job.owner is not None and job.owner != owner
        )
        if job is None or job.scope != scope or owner_mismatch:
            raise NotFoundError("ingest_job", job_id)
        return job

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=False)

    def _find_existing(
        self,
        scope: Scope,
        payload_id: str,
        key: _PayloadKey,
        *,
        owner: Scope | None = None,
    ) -> IngestJob | None:
        existing = self._state_store.find_by_payload(
            payload_id,
            scope=scope,
            owner=owner,
        )
        if existing is not None:
            if existing.scope != scope:
                return None
            if owner is not None and existing.owner is not None and existing.owner != owner:
                raise PermissionDeniedError("job_state")
            if existing.id not in self._jobs:
                existing = self._load(
                    scope,
                    existing.id,
                    owner=owner,
                    persisted=existing,
                )
                if existing is None:
                    return None
            self._jobs[existing.id] = existing
            self._job_id_by_payload[key] = existing.id
        return existing

    def _run(self, job_id: str, task: IngestTask) -> None:
        self._update(job_id, status="running")
        try:
            units = task()
        except Exception as exc:
            logger.exception("Ingest job failed: job_id=%s", job_id)
            self._update(job_id, status="failed", error=str(exc))
            return
        self._update(
            job_id,
            status="succeeded",
            unit_ids=tuple(unit.id for unit in units),
        )

    def _update(
        self,
        job_id: str,
        *,
        status: str,
        unit_ids: tuple[str, ...] = (),
        error: str = "",
    ) -> None:
        with self._lock:
            current = self._jobs[job_id]
            updated = replace(
                current,
                status=status,
                updated_at=datetime.now(timezone.utc),
                unit_ids=unit_ids,
                error=error,
            )
            self._jobs[job_id] = updated
            self._persist(updated)

    def _persist(self, job: IngestJob) -> None:
        self._state_store.save(job, owner=job.owner)

    def _load(
        self,
        scope: Scope,
        job_id: str,
        *,
        owner: Scope | None = None,
        persisted: IngestJob | None = None,
    ) -> IngestJob | None:
        job = persisted or self._state_store.get(job_id, scope=scope, owner=owner)
        if job is None:
            return None
        if job.status in {"pending", "running"}:
            job = replace(
                job,
                status="failed",
                updated_at=datetime.now(timezone.utc),
                error="ingest job was interrupted by server restart",
            )
            self._persist(job)
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


@IngestJobProducer.register("in_process")
def _build(config):
    if "state_store" in config.params:
        state_store = JobStateStoreProducer.dep(config, "state_store", default="memory")
    elif "kv_store" in config.params:
        state_store = JobStateStoreProducer.build(
            "kv",
            {"kv_store": config.params["kv_store"]},
            config.ctx,
        )
    else:
        state_store = JobStateStoreProducer.build("memory", {}, config.ctx)
    return InProcessIngestJobController(
        max_workers=int(config.get("ingest_max_workers", 1)),
        max_pending_jobs=int(config.get("ingest_max_pending_jobs", 2)),
        state_store=state_store,
    )
