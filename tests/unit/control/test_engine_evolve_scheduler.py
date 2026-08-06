from __future__ import annotations

import asyncio

import pytest

from api.memory_api_impl import build_kernel
from common.type_def import Scope
from construction import EvolveMode, Evolver, EvolveResult
from construction.base import OperatorType
from control.engine_impl.in_memory_engine import InMemoryEngine
from control.jobs import Job, JobFactory, JobType
from control.types import Channel, JobInfo, JobStatus
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from tests.conftest import sec

pytestmark = pytest.mark.unit


class RaisingEvolver(Evolver):
    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units, mode: EvolveMode) -> EvolveResult:
        raise AssertionError("Engine.evolve should delegate execution to Scheduler")


class RecordingScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[Job, Channel]] = []

    async def submit(self, job: Job, channel: Channel) -> str:
        self.calls.append((job, channel))
        return "job-1"


class _NeverRunJob(Job):
    async def run(self) -> JobInfo:
        raise AssertionError("Engine.evolve must only submit the constructed job")


class RecordingJobFactory(JobFactory):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[JobType, Scope, dict, Job]] = []

    def get_job(self, job_type: JobType, scope: Scope, **kwargs) -> Job:
        job = _NeverRunJob(scope=scope)
        self.calls.append((job_type, scope, kwargs, job))
        return job


def test_engine_evolve_only_submits_scheduler_job() -> None:
    scope = Scope(user="u1")
    scheduler = RecordingScheduler()
    job_factory = RecordingJobFactory()
    engine = InMemoryEngine(
        ingestor=None,
        index_builder=None,
        retriever=None,
        kv=InMemoryKVStore(),
        scheduler=scheduler,
        evolver=RaisingEvolver(),
        lifecycle=None,
        job_factory=job_factory,
    )

    job_id = asyncio.run(engine.evolve(scope, EvolveMode.CONSOLIDATE, Channel.HOT))

    assert job_id == "job-1"
    assert len(job_factory.calls) == 1
    job_type, requested_scope, kwargs, job = job_factory.calls[0]
    assert job_type == JobType.EVOLVE
    assert requested_scope == scope
    assert kwargs == {"mode": EvolveMode.CONSOLIDATE}
    assert scheduler.calls == [(job, Channel.HOT)]


def test_api_evolve_returns_completed_scheduler_job_with_evolve_result_detail() -> None:
    kernel = build_kernel()
    scope = Scope(user="u1")
    kernel.api.write("Alice likes tea", scope, security=sec(scope))

    job_id = kernel.api.evolve(scope, EvolveMode.EXTRACT, security=sec(scope))

    job = kernel.api.job_status(job_id, security=sec(scope))
    assert job.status == JobStatus.SUCCEEDED
    assert job.detail["created_ids"] is not None
    assert job.detail["updated_ids"] == ""
    assert job.detail["superseded_ids"] == ""
    assert job.detail["forgotten_ids"] == ""
