# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""JobStateStore implementations.

The controller only sees :class:`JobStateStore`.  This module owns the
infrastructure details for the built-in memory and KV-backed implementations:
key prefixes, JSON encoding, TTL and payload-idempotency mapping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import RLock

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.ingest_job import IngestJob
from jiuwen_memory.control.job_state import JobStateStore, JobStateStoreProducer
from jiuwen_memory.storage.kv import KvProducer, KVStore

_JOB_KEY_PREFIX = "/ingest/jobs/"
_PAYLOAD_KEY_PREFIX = "/ingest/payloads/"
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def _scope_key(scope: Scope) -> tuple[str, str, str, str, str]:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def _scope_from_dict(data: object, fallback: Scope) -> Scope:
    if not isinstance(data, dict):
        return fallback
    return Scope(
        org=str(data.get("org", "")),
        space=str(data.get("space", "")),
        user=str(data.get("user", "")),
        agent=str(data.get("agent", "")),
        session=str(data.get("session", "")),
    )


def _scope_to_dict(scope: Scope) -> dict[str, str]:
    return {
        "org": scope.org,
        "space": scope.space,
        "user": scope.user,
        "agent": scope.agent,
        "session": scope.session,
    }


def _owner_matches(stored: Scope | None, requested: Scope | None) -> bool:
    """Treat an omitted owner as an unfiltered read/write by the API boundary."""
    if stored is None or requested is None:
        return True
    return _scope_key(stored) == _scope_key(requested)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validate_ttl(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    try:
        ttl = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be non-negative") from exc
    if ttl < 0:
        raise ValidationError(f"{name} must be non-negative")
    return ttl


def _expiry_for(
    job: IngestJob,
    ttl_seconds: float,
    terminal_ttl_seconds: float | None,
) -> datetime | None:
    ttl = (
        terminal_ttl_seconds
        if job.status in _TERMINAL_STATUSES and terminal_ttl_seconds is not None
        else ttl_seconds
    )
    if ttl <= 0:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=ttl)


@dataclass
class _MemoryEntry:
    job: IngestJob
    expires_at: datetime | None


class InMemoryJobStateStore(JobStateStore):
    """Process-local JobStateStore used by direct/controller unit tests."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 0.0,
        terminal_ttl_seconds: float | None = None,
    ) -> None:
        self._ttl_seconds = _validate_ttl(ttl_seconds, "ttl_seconds") or 0.0
        self._terminal_ttl_seconds = _validate_ttl(
            terminal_ttl_seconds, "terminal_ttl_seconds"
        )
        self._jobs: dict[tuple[tuple[str, str, str, str, str], str], _MemoryEntry] = {}
        self._payloads: dict[tuple[tuple[str, str, str, str, str], str], str] = {}
        self._lock = RLock()

    def save(
        self,
        job: IngestJob,
        *,
        scope: Scope | None = None,
        owner: Scope | None = None,
    ) -> None:
        target_scope = scope or job.scope
        if _scope_key(target_scope) != _scope_key(job.scope):
            raise ValidationError("job state scope differs from persisted job scope")
        record = self._with_owner(job, owner)
        job_key = (_scope_key(target_scope), record.id)
        payload_key = (_scope_key(target_scope), record.payload_id)
        with self._lock:
            self._purge_expired_locked()
            existing_entry = self._jobs.get(job_key)
            if existing_entry is not None and not _owner_matches(
                existing_entry.job.owner, record.owner
            ):
                raise PermissionDeniedError("job_state")
            existing_id = self._payloads.get(payload_key)
            if existing_id is not None and existing_id != record.id:
                mapped = self._jobs.get((payload_key[0], existing_id))
                if mapped is not None and mapped.job.status not in {"failed", "cancelled"}:
                    raise ConflictError("ingest_payload", record.payload_id)
            self._jobs[job_key] = _MemoryEntry(
                record,
                _expiry_for(record, self._ttl_seconds, self._terminal_ttl_seconds),
            )
            self._payloads[payload_key] = record.id

    def get(
        self,
        job_id: str,
        *,
        scope: Scope,
        owner: Scope | None = None,
    ) -> IngestJob | None:
        with self._lock:
            self._purge_expired_locked()
            entry = self._jobs.get((_scope_key(scope), job_id))
            if entry is None or not _owner_matches(entry.job.owner, owner):
                return None
            return entry.job

    def find_by_payload(
        self,
        payload_id: str,
        *,
        scope: Scope,
        owner: Scope | None = None,
    ) -> IngestJob | None:
        with self._lock:
            self._purge_expired_locked()
            job_id = self._payloads.get((_scope_key(scope), payload_id))
            if job_id is None:
                return None
            entry = self._jobs.get((_scope_key(scope), job_id))
            if entry is None:
                self._payloads.pop((_scope_key(scope), payload_id), None)
                return None
            if not _owner_matches(entry.job.owner, owner):
                return None
            return entry.job

    def delete(
        self,
        job_id: str,
        *,
        scope: Scope,
        owner: Scope | None = None,
    ) -> None:
        with self._lock:
            self._purge_expired_locked()
            key = (_scope_key(scope), job_id)
            entry = self._jobs.get(key)
            if entry is None:
                return
            if not _owner_matches(entry.job.owner, owner):
                raise PermissionDeniedError("job_state")
            self._jobs.pop(key, None)
            payload_key = (_scope_key(scope), entry.job.payload_id)
            if self._payloads.get(payload_key) == job_id:
                self._payloads.pop(payload_key, None)

    def cleanup(
        self,
        scope: Scope,
        *,
        older_than: datetime | None = None,
        owner: Scope | None = None,
    ) -> int:
        removed = 0
        scope_key = _scope_key(scope)
        threshold = _utc(older_than) if older_than is not None else None
        with self._lock:
            self._purge_expired_locked()
            for key, entry in list(self._jobs.items()):
                if key[0] != scope_key or not _owner_matches(entry.job.owner, owner):
                    continue
                if threshold is None or _utc(entry.job.updated_at) >= threshold:
                    continue
                self._jobs.pop(key, None)
                payload_key = (scope_key, entry.job.payload_id)
                if self._payloads.get(payload_key) == entry.job.id:
                    self._payloads.pop(payload_key, None)
                removed += 1
        return removed

    def _with_owner(self, job: IngestJob, owner: Scope | None) -> IngestJob:
        if owner is None:
            return job
        if job.owner is not None and not _owner_matches(job.owner, owner):
            raise PermissionDeniedError("job_state")
        return job if job.owner is not None else replace(job, owner=owner)

    def _purge_expired_locked(self) -> None:
        now = datetime.now(timezone.utc)
        for key, entry in list(self._jobs.items()):
            if entry.expires_at is None or entry.expires_at > now:
                continue
            self._jobs.pop(key, None)
            payload_key = (key[0], entry.job.payload_id)
            if self._payloads.get(payload_key) == entry.job.id:
                self._payloads.pop(payload_key, None)


class KVJobStateStore(JobStateStore):
    """KV-backed JobStateStore; all KV details stay inside this adapter."""

    def __init__(
        self,
        kv: KVStore,
        *,
        ttl_seconds: float = 0.0,
        terminal_ttl_seconds: float | None = None,
    ) -> None:
        self._kv = kv
        self._ttl_seconds = _validate_ttl(ttl_seconds, "ttl_seconds") or 0.0
        self._terminal_ttl_seconds = _validate_ttl(
            terminal_ttl_seconds, "terminal_ttl_seconds"
        )
        self._lock = RLock()

    def save(
        self,
        job: IngestJob,
        *,
        scope: Scope | None = None,
        owner: Scope | None = None,
    ) -> None:
        target_scope = scope or job.scope
        if _scope_key(target_scope) != _scope_key(job.scope):
            raise ValidationError("job state scope differs from persisted job scope")
        record = self._with_owner(job, owner)
        with self._lock:
            existing = self._read_job(target_scope, record.id)
            if existing is not None and not _owner_matches(existing.owner, record.owner):
                raise PermissionDeniedError("job_state")
            mapped_id = self._read_payload_id(target_scope, record.payload_id)
            if mapped_id is not None and mapped_id != record.id:
                mapped = self._read_job(target_scope, mapped_id)
                if mapped is not None and mapped.status not in {"failed", "cancelled"}:
                    raise ConflictError("ingest_payload", record.payload_id)
            ttl = _expiry_ttl(record, self._ttl_seconds, self._terminal_ttl_seconds)
            self._write(
                target_scope,
                _job_storage_key(record.id),
                _encode_job(record),
                ttl,
            )
            self._write(
                target_scope,
                _payload_storage_key(record.payload_id),
                record.id.encode("utf-8"),
                ttl,
            )

    def get(
        self,
        job_id: str,
        *,
        scope: Scope,
        owner: Scope | None = None,
    ) -> IngestJob | None:
        with self._lock:
            job = self._read_job(scope, job_id)
            if job is None or not _owner_matches(job.owner, owner):
                return None
            return job

    def find_by_payload(
        self,
        payload_id: str,
        *,
        scope: Scope,
        owner: Scope | None = None,
    ) -> IngestJob | None:
        with self._lock:
            job_id = self._read_payload_id(scope, payload_id)
            if job_id is None:
                return None
            job = self._read_job(scope, job_id)
            if job is None:
                self._kv.delete(scope, _payload_storage_key(payload_id))
                return None
            if not _owner_matches(job.owner, owner):
                return None
            return job

    def delete(
        self,
        job_id: str,
        *,
        scope: Scope,
        owner: Scope | None = None,
    ) -> None:
        with self._lock:
            job = self._read_job(scope, job_id)
            if job is None:
                return
            if not _owner_matches(job.owner, owner):
                raise PermissionDeniedError("job_state")
            self._kv.delete(scope, _job_storage_key(job_id))
            payload_key = _payload_storage_key(job.payload_id)
            if self._read_payload_id(scope, job.payload_id) == job_id:
                self._kv.delete(scope, payload_key)

    def cleanup(
        self,
        scope: Scope,
        *,
        older_than: datetime | None = None,
        owner: Scope | None = None,
    ) -> int:
        if older_than is None:
            return 0
        threshold = _utc(older_than)
        removed = 0
        with self._lock:
            for key, raw in self._kv.scan(scope, prefix=_JOB_KEY_PREFIX):
                job_id = key[len(_JOB_KEY_PREFIX):]
                job = _decode_job(raw, scope)
                if not _owner_matches(job.owner, owner):
                    continue
                if _utc(job.updated_at) >= threshold:
                    continue
                self.delete(job_id, scope=scope, owner=owner)
                removed += 1
        return removed

    def health(self) -> None:
        self._kv.health()

    def _with_owner(self, job: IngestJob, owner: Scope | None) -> IngestJob:
        if owner is None:
            return job
        if job.owner is not None and not _owner_matches(job.owner, owner):
            raise PermissionDeniedError("job_state")
        return job if job.owner is not None else replace(job, owner=owner)

    def _read_job(self, scope: Scope, job_id: str) -> IngestJob | None:
        key = _job_storage_key(job_id)
        if not self._kv.exists(scope, key):
            return None
        try:
            return _decode_job(self._kv.get(scope, key), scope)
        except (KeyError, TypeError, ValueError) as exc:
            raise BackendError(f"invalid persisted ingest job {job_id!r}") from exc

    def _read_payload_id(self, scope: Scope, payload_id: str) -> str | None:
        key = _payload_storage_key(payload_id)
        if not self._kv.exists(scope, key):
            return None
        return self._kv.get(scope, key).decode("utf-8")

    def _write(self, scope: Scope, key: str, value: bytes, ttl: float) -> None:
        if self._kv.exists(scope, key):
            self._kv.update(scope, key, value, ttl=ttl)
        else:
            self._kv.insert(scope, key, value, ttl=ttl)


def _expiry_ttl(
    job: IngestJob,
    ttl_seconds: float,
    terminal_ttl_seconds: float | None,
) -> float:
    if job.status in _TERMINAL_STATUSES and terminal_ttl_seconds is not None:
        return terminal_ttl_seconds
    return ttl_seconds


def _encode_job(job: IngestJob) -> bytes:
    data = {
        "id": job.id,
        "payload_id": job.payload_id,
        "source_ref": job.source_ref,
        "scope": _scope_to_dict(job.scope),
        "owner": _scope_to_dict(job.owner) if job.owner is not None else None,
        "status": job.status,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "unit_ids": list(job.unit_ids),
        "error": job.error,
    }
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decode_job(raw: bytes, fallback_scope: Scope) -> IngestJob:
    data = json.loads(raw.decode("utf-8"))
    owner_raw = data.get("owner")
    owner = _scope_from_dict(owner_raw, Scope()) if isinstance(owner_raw, dict) else None
    return IngestJob(
        id=str(data["id"]),
        payload_id=str(data["payload_id"]),
        source_ref=str(data["source_ref"]),
        scope=_scope_from_dict(data.get("scope"), fallback_scope),
        owner=owner,
        status=str(data["status"]),
        created_at=datetime.fromisoformat(str(data["created_at"])),
        updated_at=datetime.fromisoformat(str(data["updated_at"])),
        unit_ids=tuple(str(item) for item in data.get("unit_ids", [])),
        error=str(data.get("error", "")),
    )


def _job_storage_key(job_id: str) -> str:
    return f"{_JOB_KEY_PREFIX}{job_id}"


def _payload_storage_key(payload_id: str) -> str:
    return f"{_PAYLOAD_KEY_PREFIX}{payload_id}"


@JobStateStoreProducer.register("memory")
def _build_memory(config):
    return InMemoryJobStateStore(
        ttl_seconds=float(config.get("job_state_ttl_seconds", 0.0)),
        terminal_ttl_seconds=_optional_float(config.get("job_state_terminal_ttl_seconds")),
    )


@JobStateStoreProducer.register("kv")
def _build_kv(config):
    return KVJobStateStore(
        KvProducer.dep(config, "kv_store", default="memory"),
        ttl_seconds=float(config.get("job_state_ttl_seconds", 0.0)),
        terminal_ttl_seconds=_optional_float(config.get("job_state_terminal_ttl_seconds")),
    )


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
