"""In-process background jobs for long-running ingest requests."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import BoundedSemaphore, RLock

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    NotFoundError,
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
from jiuwen_memory.storage.kv import KvProducer, KVStore

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
        self._job_id_by_payload: dict[_PayloadKey, str] = {}
        self._lock = RLock()
        self._closed = False

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
        now = datetime.now(timezone.utc)
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

    def status(self, job_id: str, *, scope: Scope) -> IngestJob:
        with self._lock:
            job = self._jobs.get(job_id) or self._load(scope, job_id)
        if job is None or job.scope != scope:
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
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        job_key = _job_storage_key(job.id)
        if self._kv.exists(job.scope, job_key):
            self._kv.update(job.scope, job_key, value)
        else:
            self._kv.insert(job.scope, job_key, value)
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
            data = json.loads(
                self._kv.get(scope, _job_storage_key(job_id)).decode("utf-8")
            )
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
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BackendError(f"invalid persisted ingest job {job_id!r}") from exc
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


def _job_storage_key(job_id: str) -> str:
    return f"{_JOB_KEY_PREFIX}{job_id}"


def _payload_storage_key(payload_id: str) -> str:
    return f"{_PAYLOAD_KEY_PREFIX}{payload_id}"


@IngestJobProducer.register("in_process")
def _build(config):
    return InProcessIngestJobController(
        max_workers=int(config.get("ingest_max_workers", 1)),
        max_pending_jobs=int(config.get("ingest_max_pending_jobs", 2)),
        kv=KvProducer.dep(config, default="memory"),
    )
