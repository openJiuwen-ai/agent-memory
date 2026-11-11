"""Ingest cancellation must pass the same trusted-context and WRITE PEP as evolve."""

# White-box fault injection verifies persistence and authorization boundaries.
# pylint: disable=protected-access

from datetime import UTC, datetime
from threading import Event

import pytest

from jiuwen_memory.api import (
    BackendError,
    Modality,
    NotFoundError,
    PermissionDeniedError,
    Scope,
    ValidationError,
    assemble_runtime,
)
from jiuwen_memory.common.security.types import Action
from jiuwen_memory.control.ingest_job import IngestJob
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from tests.unit.api.fixtures import security_for

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("method", "failure"),
    [("job_status", "authorize"), ("job_cancel", "authorize"), ("job_cancel", "audit")],
)
def test_recovery_lookup_before_denial_never_mutates_storage(monkeypatch, method, failure) -> None:
    kv = InMemoryKVStore()
    runtime = assemble_runtime(kv=kv)
    api = runtime.api
    scope = Scope(org="acme", user="alice")
    security = security_for(api, scope)
    jobs = api._ingest_jobs
    now = datetime.now(UTC)
    job = IngestJob("ing_interrupted", "p", "s", scope, "running", now, now)
    jobs._persist(job, update_payload=True)
    key = f"/ingest/jobs/{job.id}"
    before = kv.get(scope, key)

    def reject(*args, **kwargs):
        raise PermissionDeniedError("test denied")

    try:
        projected = jobs.status(job.id, scope=scope)
        assert projected.status == "failed" and projected.retry_requires_reconciliation
        assert kv.get(scope, key) == before
        monkeypatch.setattr(
            api, "_authorize" if failure == "authorize" else "_ensure_audit_integrity_healthy",
            reject,
        )
        with pytest.raises(PermissionDeniedError, match="test denied"):
            getattr(api, method)(job.id, scope=scope, security=security)
        assert kv.get(scope, key) == before
        assert not jobs._jobs and not jobs._job_id_by_payload
    finally:
        runtime.close()


def test_ingest_cancel_requires_scope_and_write_authorization(monkeypatch) -> None:
    runtime = assemble_runtime()
    api = runtime.api
    scope = Scope(org="acme", user="alice")
    security = security_for(api, scope)
    started, release = Event(), Event()

    def add(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(api, "add", add)
    try:
        job = api.submit_ingest(
            "text", scope, Modality.TEXT, security=security,
            payload_id="p", source_ref="s",
        ).job
        assert started.wait(2)
        with pytest.raises(ValidationError, match="target scope"):
            api.job_cancel(job.id, security=security)
        with pytest.raises(NotFoundError):
            api.job_cancel(job.id, scope=Scope(org="acme", user="bob"), security=security)
        authorize = api._authorize

        def reject_write(security, target, action, *args, **kwargs):
            if action == Action.WRITE:
                raise PermissionDeniedError("WRITE denied")
            return authorize(security, target, action, *args, **kwargs)

        monkeypatch.setattr(api, "_authorize", reject_write)
        with pytest.raises(PermissionDeniedError):
            api.job_cancel(job.id, scope=scope, security=security)
        assert api.job_status(job.id, scope=scope, security=security).detail[
            "cancel_requested"
        ] == "false"
        monkeypatch.setattr(api, "_authorize", authorize)
        health = api._ensure_audit_integrity_healthy

        def unhealthy():
            raise BackendError("audit unavailable")

        monkeypatch.setattr(api, "_ensure_audit_integrity_healthy", unhealthy)
        with pytest.raises(BackendError, match="audit unavailable"):
            api.job_cancel(job.id, scope=scope, security=security)
        assert api.job_status(job.id, scope=scope, security=security).detail[
            "cancel_requested"
        ] == "false"
        monkeypatch.setattr(api, "_ensure_audit_integrity_healthy", health)
        api.job_cancel(job.id, scope=scope, security=security)
        assert api.job_status(job.id, scope=scope, security=security).detail[
            "cancel_requested"
        ] == "true"
        release.set()
    finally:
        release.set()
        runtime.close()
