from __future__ import annotations

from datetime import datetime

from common.type_def import MemoryUnit, Scope, Segment, memory_key
from common.type_def.memory_codec import dumps
from construction import EvolveMode, Evolver, EvolveResult
from construction.base import OperatorType
from control.scheduler_impl.in_process_scheduler import InProcessScheduler
from control.types import Channel, JobInfo, JobStatus
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore


class RecordingEvolver(Evolver):
    def __init__(self) -> None:
        self.calls: list[tuple[list[MemoryUnit], EvolveMode]] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        self.calls.append((units, mode))
        return EvolveResult(
            created_ids=["created-1"],
            updated_ids=["updated-1"],
            superseded_ids=["old-1"],
            forgotten_ids=["forgotten-1"],
        )


def test_submit_runs_job_to_success_with_sync_state_flow() -> None:
    class RecordingScheduler(InProcessScheduler):
        def __init__(self) -> None:
            super().__init__()
            self.seen_statuses: list[JobStatus] = []

        def _execute_task(self, job: JobInfo) -> None:
            self.seen_statuses.append(job.status)

    scheduler = RecordingScheduler()

    job_id = scheduler.submit(Scope(user="u1"), EvolveMode.EXTRACT, Channel.HOT)

    job = scheduler.status(job_id)
    assert scheduler.seen_statuses == [JobStatus.RUNNING]
    assert job.status == JobStatus.SUCCEEDED
    assert job.detail["started_at"]
    assert job.detail["finished_at"]
    assert datetime.fromisoformat(job.detail["started_at"]) <= datetime.fromisoformat(
        job.detail["finished_at"]
    )


def test_submit_records_failed_job_and_returns_job_id_when_execution_raises() -> None:
    class FailingScheduler(InProcessScheduler):
        def _execute_task(self, job: JobInfo) -> None:
            raise RuntimeError("scheduler boom")

    scheduler = FailingScheduler()

    job_id = scheduler.submit(Scope(user="u1"), EvolveMode.CONSOLIDATE, Channel.BACKGROUND)

    job = scheduler.status(job_id)
    assert job.status == JobStatus.FAILED
    assert job.detail["error_type"] == "RuntimeError"
    assert job.detail["error"] == "scheduler boom"
    assert job.detail["started_at"]
    assert job.detail["finished_at"]


def test_submit_executes_evolver_with_units_from_scope_and_records_result() -> None:
    scope = Scope(user="u1")
    other_scope = Scope(user="u2")
    kv = InMemoryKVStore()
    kv.insert(
        scope,
        memory_key("unit-1"),
        dumps(MemoryUnit(id="unit-1", scope=scope, segments=[Segment(content="one")])),
    )
    kv.insert(
        scope,
        memory_key("unit-2"),
        dumps(MemoryUnit(id="unit-2", scope=scope, segments=[Segment(content="two")])),
    )
    kv.insert(
        other_scope,
        memory_key("other-unit"),
        dumps(MemoryUnit(id="other-unit", scope=other_scope, segments=[Segment(content="other")])),
    )
    evolver = RecordingEvolver()
    scheduler = InProcessScheduler(kv=kv, evolver=evolver)

    job_id = scheduler.submit(scope, EvolveMode.ASSOCIATE, Channel.BACKGROUND)

    job = scheduler.status(job_id)
    assert job.status == JobStatus.SUCCEEDED
    assert len(evolver.calls) == 1
    units, mode = evolver.calls[0]
    assert mode == EvolveMode.ASSOCIATE
    assert {unit.id for unit in units} == {"unit-1", "unit-2"}
    assert job.detail["created_ids"] == "created-1"
    assert job.detail["updated_ids"] == "updated-1"
    assert job.detail["superseded_ids"] == "old-1"
    assert job.detail["forgotten_ids"] == "forgotten-1"


def test_cancel_is_idempotent_and_does_not_change_completed_jobs() -> None:
    scheduler = InProcessScheduler()
    job_id = scheduler.submit(Scope(user="u1"), EvolveMode.EXTRACT, Channel.BACKGROUND)

    scheduler.cancel(job_id)
    scheduler.cancel("missing-job")

    assert scheduler.status(job_id).status == JobStatus.SUCCEEDED
