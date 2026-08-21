from __future__ import annotations

import time
from threading import Event

import pytest

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError
from jiuwen_memory.common.type_def import MemoryUnit, Modality, Scope, Segment
from jiuwen_memory.control.job_impl.ingest_job import InProcessIngestJobController
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
    writer = InProcessIngestJobController(kv=kv)
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

    reader = InProcessIngestJobController(kv=kv)
    try:
        job = reader.status(submission.job.id, scope=scope)

        assert job.status == "succeeded"
        assert not vars(reader).get("_jobs")
        assert not vars(reader).get("_job_id_by_payload")
    finally:
        reader.close()
