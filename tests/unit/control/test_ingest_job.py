from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError
from jiuwen_memory.common.type_def import MemoryUnit, Modality, Scope, Segment
from jiuwen_memory.control.ingest_job import IngestJob
from jiuwen_memory.control.job_impl.ingest_job import InProcessIngestJobController
from jiuwen_memory.control.job_impl.job_state import (
    InMemoryJobStateStore,
    KVJobStateStore,
)
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit


def test_ingest_job_runs_in_background_and_reuses_payload() -> None:
    scope = Scope(user="user-1")
    started = Event()
    release = Event()
    controller = InProcessIngestJobController(max_workers=1, max_pending_jobs=1)

    def task() -> list[MemoryUnit]:
        started.set()
        assert release.wait(2)
        return [
            MemoryUnit(
                id="unit-1",
                scope=scope,
                segments=[Segment(content="video memory", source=Modality.VIDEO)],
            )
        ]

    try:
        first = controller.submit(
            payload_id="video-1",
            source_ref="file:///data/demo.mp4",
            scope=scope,
            task=task,
        )
        assert first.job.id.startswith("ing_")
        assert started.wait(1)

        duplicate = controller.submit(
            payload_id="video-1",
            source_ref="file:///data/demo.mp4",
            scope=scope,
            task=task,
        )
        assert duplicate.reused is True
        assert duplicate.job.id == first.job.id

        with pytest.raises(ConflictError):
            controller.submit(
                payload_id="video-1",
                source_ref="file:///data/other.mp4",
                scope=scope,
                task=task,
            )

        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = controller.status(first.job.id, scope=scope)
            if job.status == "succeeded":
                break
            time.sleep(0.01)

        assert job.status == "succeeded"
        assert job.unit_ids == ("unit-1",)
    finally:
        release.set()
        controller.close()


def test_ingest_payload_idempotency_isolated_by_space() -> None:
    controller = InProcessIngestJobController(max_workers=1, max_pending_jobs=1)
    try:
        first = controller.submit(
            payload_id="video-1",
            source_ref="file:///data/a.mp4",
            scope=Scope(org="org-1", space="space-a", user="user-1"),
            task=lambda: [],
        )
        second = controller.submit(
            payload_id="video-1",
            source_ref="file:///data/b.mp4",
            scope=Scope(org="org-1", space="space-b", user="user-1"),
            task=lambda: [],
        )
        assert second.reused is False
        assert second.job.id != first.job.id
    finally:
        controller.close()


def test_failed_ingest_allows_payload_retry_with_corrected_source() -> None:
    scope = Scope(user="user-1")
    controller = InProcessIngestJobController(max_workers=1, max_pending_jobs=1)
    try:
        failed = controller.submit(
            payload_id="video-1",
            source_ref="file:///data/missing.mp4",
            scope=scope,
            task=lambda: (_ for _ in ()).throw(BackendError("missing video")),
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if controller.status(failed.job.id, scope=scope).status == "failed":
                break
            time.sleep(0.01)

        retried = controller.submit(
            payload_id="video-1",
            source_ref="file:///data/correct.mp4",
            scope=scope,
            task=lambda: [],
        )

        assert retried.reused is False
        assert retried.job.id != failed.job.id
        assert retried.job.source_ref == "file:///data/correct.mp4"
    finally:
        controller.close()


def test_ingest_job_status_does_not_pollute_cross_scope_payload_mapping() -> None:
    """P1-1: 跨 scope 查 job_id 不得污染 payload 映射；同 payload_id 提交不复用。"""
    scope_a = Scope(org="tnt", user="alice")
    scope_b = Scope(org="tnt", user="bob")
    controller = InProcessIngestJobController(max_workers=1, max_pending_jobs=2)
    try:
        # A 提交任务
        sub_a = controller.submit(
            payload_id="video-001",
            source_ref="file:///a.mp4",
            scope=scope_a,
            task=lambda: [],
        )
        # B 偷到 A 的 job_id，用 scope B 查 → NotFoundError（不得泄露）
        with pytest.raises(NotFoundError):
            controller.status(sub_a.job.id, scope=scope_b)
        # B 用相同 payload_id 提交自己的视频 → 不应复用 A 的任务
        sub_b = controller.submit(
            payload_id="video-001",
            source_ref="file:///b.mp4",
            scope=scope_b,
            task=lambda: [],
        )
        assert sub_b.reused is False
        assert sub_b.job.id != sub_a.job.id
    finally:
        controller.close()


def test_ingest_job_status_does_not_populate_idempotency_cache() -> None:
    """状态读取不应在 READ 鉴权前修改进程缓存或 payload 映射。"""
    scope = Scope(org="tnt", user="alice")
    kv = InMemoryKVStore()
    writer = InProcessIngestJobController(state_store=KVJobStateStore(kv))
    try:
        submission = writer.submit(
            payload_id="video-001",
            source_ref="file:///a.mp4",
            scope=scope,
            task=lambda: [],
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if writer.status(submission.job.id, scope=scope).status == "succeeded":
                break
            time.sleep(0.01)
    finally:
        writer.close()

    reader = InProcessIngestJobController(state_store=KVJobStateStore(kv))
    try:
        job = reader.status(submission.job.id, scope=scope)

        assert job.status == "succeeded"
        assert not vars(reader).get("_jobs")
        assert not vars(reader).get("_job_id_by_payload")
    finally:
        reader.close()


def test_ingest_job_status_refreshes_an_existing_cache_from_state_store() -> None:
    scope = Scope(org="org-1", space="space-1", user="user-1")
    state_store = InMemoryJobStateStore()
    controller = InProcessIngestJobController(state_store=state_store)
    try:
        stale = _persisted_job(
            scope=scope,
            job_id="ing_refresh",
            payload_id="payload-refresh",
            status="pending",
        )
        current = _persisted_job(
            scope=scope,
            job_id=stale.id,
            payload_id=stale.payload_id,
            status="succeeded",
        )
        state_store.save(stale)
        controller.status(stale.id, scope=scope)
        state_store.save(current)

        loaded = controller.status(stale.id, scope=scope)

        assert loaded == current
    finally:
        controller.close()


def _persisted_job(
    *,
    scope: Scope,
    job_id: str = "ing_test",
    payload_id: str = "payload-test",
    status: str = "succeeded",
    updated_at: datetime | None = None,
    owner: Scope | None = None,
) -> IngestJob:
    created_at = datetime.now(timezone.utc)
    return IngestJob(
        id=job_id,
        payload_id=payload_id,
        source_ref="file:///data/demo.mp4",
        scope=scope,
        owner=owner,
        status=status,
        created_at=created_at,
        updated_at=updated_at or created_at,
    )


def test_job_state_store_enforces_full_scope_and_owner() -> None:
    scope = Scope(org="org-1", space="space-1", user="user-1", agent="agent-1", session="s-1")
    other_scope = Scope(org="org-1", space="space-2", user="user-1", agent="agent-1", session="s-1")
    owner = Scope(org="org-1", user="owner")
    outsider = Scope(org="org-1", user="outsider")
    store = InMemoryJobStateStore()
    job = _persisted_job(scope=scope, owner=owner)

    store.save(job)

    assert store.get(job.id, scope=scope, owner=owner) == job
    # API job_status reads the task before applying its own READ decision, so
    # omitting owner must not hide an otherwise scope-matching record.
    assert store.get(job.id, scope=scope) == job
    assert store.find_by_payload(job.payload_id, scope=other_scope, owner=owner) is None
    assert store.get(job.id, scope=scope, owner=outsider) is None


def test_ingest_job_health_delegates_to_state_store() -> None:
    state_store = Mock()
    controller = InProcessIngestJobController(state_store=state_store)
    try:
        controller.health()
    finally:
        controller.close()

    state_store.health.assert_called_once_with()


def test_job_state_store_ttl_and_explicit_cleanup() -> None:
    scope = Scope(org="org-1", space="space-1", user="user-1")
    ttl_store = InMemoryJobStateStore(ttl_seconds=0.02)
    expiring = _persisted_job(scope=scope, job_id="ing_expiring", payload_id="payload-expiring")
    ttl_store.save(expiring)
    time.sleep(0.04)
    assert ttl_store.get(expiring.id, scope=scope) is None
    assert ttl_store.find_by_payload(expiring.payload_id, scope=scope) is None

    cleanup_store = InMemoryJobStateStore()
    now = datetime.now(timezone.utc)
    old = _persisted_job(
        scope=scope,
        job_id="ing_old",
        payload_id="payload-old",
        updated_at=now - timedelta(seconds=10),
    )
    current = _persisted_job(
        scope=scope,
        job_id="ing_current",
        payload_id="payload-current",
        updated_at=now,
    )
    cleanup_store.save(old)
    cleanup_store.save(current)

    assert cleanup_store.cleanup(scope, older_than=now - timedelta(seconds=1)) == 1
    assert cleanup_store.get(old.id, scope=scope) is None
    assert cleanup_store.get(current.id, scope=scope) == current


def test_kv_job_state_store_preserves_restart_interruption_semantics() -> None:
    scope = Scope(org="org-1", space="space-1", user="user-1")
    kv = InMemoryKVStore()
    state_store = KVJobStateStore(kv)
    pending = _persisted_job(
        scope=scope,
        job_id="ing_pending",
        payload_id="payload-pending",
        status="pending",
    )
    state_store.save(pending)

    controller = InProcessIngestJobController(state_store=state_store)
    try:
        loaded = controller.status(pending.id, scope=scope)
        assert loaded.status == "failed"
        assert loaded.error == "ingest job was interrupted by server restart"
        persisted = state_store.get(pending.id, scope=scope)
        assert persisted is not None
        assert persisted.status == "failed"
    finally:
        controller.close()


def test_submit_after_restart_does_not_reuse_interrupted_pending_job() -> None:
    scope = Scope(org="org-1", space="space-1", user="user-1")
    kv = InMemoryKVStore()
    state_store = KVJobStateStore(kv)
    pending = _persisted_job(
        scope=scope,
        job_id="ing_pending_submit",
        payload_id="payload-pending-submit",
        status="pending",
    )
    state_store.save(pending)

    controller = InProcessIngestJobController(state_store=state_store)
    try:
        submission = controller.submit(
            payload_id=pending.payload_id,
            source_ref=pending.source_ref,
            scope=scope,
            task=lambda: [],
        )

        assert submission.reused is False
        assert submission.job.id != pending.id
        interrupted = state_store.get(pending.id, scope=scope)
        assert interrupted is not None
        assert interrupted.status == "failed"
        assert interrupted.error == "ingest job was interrupted by server restart"
    finally:
        controller.close()


def test_kv_job_state_store_terminal_ttl_and_scope_isolation() -> None:
    scope = Scope(org="org-1", space="space-1", user="user-1", agent="a", session="s")
    other_scope = Scope(org="org-1", space="space-1", user="user-1", agent="b", session="s")
    store = KVJobStateStore(InMemoryKVStore(), terminal_ttl_seconds=0.02)
    job = _persisted_job(scope=scope, status="succeeded")
    store.save(job)

    assert store.get(job.id, scope=other_scope) is None
    time.sleep(0.04)
    assert store.get(job.id, scope=scope) is None
