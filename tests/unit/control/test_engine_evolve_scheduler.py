from __future__ import annotations

import asyncio

from common.type_def import Scope
from construction import EvolveMode, EvolveResult, Evolver
from construction.base import OperatorType
from api.memory_api_impl import build_kernel
from control.engine_impl.in_memory_engine import InMemoryEngine
from control.types import BatchWriteItem, Channel, JobStatus
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore


class RaisingEvolver(Evolver):
    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units, mode: EvolveMode) -> EvolveResult:
        raise AssertionError("Engine.evolve should delegate execution to Scheduler")


class RecordingScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[Scope, EvolveMode, Channel]] = []

    def submit(self, scope: Scope, mode: EvolveMode, channel: Channel) -> str:
        self.calls.append((scope, mode, channel))
        return "job-1"


def test_engine_evolve_only_submits_scheduler_job() -> None:
    scope = Scope(user="u1")
    scheduler = RecordingScheduler()
    engine = InMemoryEngine(
        ingestor=None,
        index_builder=None,
        retriever=None,
        kv=InMemoryKVStore(),
        scheduler=scheduler,
        evolver=RaisingEvolver(),
        lifecycle=None,
    )

    job_id = asyncio.run(engine.evolve(scope, EvolveMode.CONSOLIDATE, Channel.HOT))

    assert job_id == "job-1"
    assert scheduler.calls == [(scope, EvolveMode.CONSOLIDATE, Channel.HOT)]


def test_in_memory_batch_write_collects_unexpected_error_and_continues() -> None:
    scope = Scope(user="u1")
    engine = InMemoryEngine(
        ingestor=None,
        index_builder=None,
        retriever=None,
        kv=InMemoryKVStore(),
        scheduler=None,
        evolver=None,
        lifecycle=None,
    )
    attempted: list[str] = []

    async def _write(content, *_args, **_kwargs):
        attempted.append(content)
        if content == "bad":
            raise RuntimeError("unavailable dependency")
        return []

    engine.write = _write  # type: ignore[method-assign]
    result = asyncio.run(
        engine.batch_write(
            [BatchWriteItem(content="bad", scope=scope), BatchWriteItem(content="good", scope=scope)]
        )
    )

    assert attempted == ["bad", "good"]
    assert result.outcomes[0].error_type == "InternalError"
    assert not result.outcomes[1].error


def test_api_evolve_returns_completed_scheduler_job_with_evolve_result_detail() -> None:
    kernel = build_kernel()
    scope = Scope(user="u1")
    kernel.api.write("Alice likes tea", scope, identity=scope)

    job_id = kernel.api.evolve(scope, EvolveMode.EXTRACT, identity=scope)

    job = kernel.api.job_status(job_id, identity=scope)
    assert job.status == JobStatus.SUCCEEDED
    assert job.detail["created_ids"] is not None
    assert job.detail["updated_ids"] == ""
    assert job.detail["superseded_ids"] == ""
    assert job.detail["forgotten_ids"] == ""
