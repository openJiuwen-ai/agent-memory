"""Deterministic ingest cancellation at real Engine execution boundaries (#285)."""

# pylint: disable=protected-access

import asyncio
from itertools import product
from threading import Event
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.control.engine_impl.agent_engine import AgentEngine
from jiuwen_memory.control.engine_impl.cloud_engine import CloudEngine
from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
from jiuwen_memory.control.ingest_job import begin_ingest_write, record_ingest_units
from jiuwen_memory.control.job_impl.ingest_job import InProcessIngestJobController
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit
SCOPE = Scope(org="acme", user="alice")
INGEST_ENGINES = (AgentEngine, CloudEngine, InMemoryEngine)
INGEST_STAGES = (
    "normalize", "classify", "write", "evolve", "evolve_read_failure", "procedural"
)


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled"])
def test_terminal_persistence_failure_keeps_real_outcome_and_ids(monkeypatch, outcome) -> None:
    jobs = InProcessIngestJobController()
    entered, release, finished = Event(), Event(), Event()
    unit = MemoryUnit(id="known-written", scope=SCOPE, segments=[Segment(content="text")])
    persist, finish = jobs._persist, jobs._finished

    def fail_terminal(job, **kwargs):
        if job.status in {"succeeded", "failed", "cancelled"}:
            raise BackendError("terminal persist failed")
        return persist(job, **kwargs)

    def observe_finish(job_id, future):
        finish(job_id, future)
        finished.set()

    def task():
        begin_ingest_write()
        record_ingest_units([unit])
        entered.set()
        assert release.wait(5)
        if outcome == "failed":
            raise ValueError("business failure")
        return [unit]

    monkeypatch.setattr(jobs, "_persist", fail_terminal)
    monkeypatch.setattr(jobs, "_finished", observe_finish)
    try:
        job = jobs.submit(payload_id="p", source_ref="s", scope=SCOPE, task=task).job
        assert entered.wait(2)
        if outcome == "cancelled":
            jobs.cancel(job.id, scope=SCOPE)
        release.set()
        assert finished.wait(3), "worker must finish even when state persistence fails"
        result = jobs.status(job.id, scope=SCOPE)
        assert result.status == outcome
        assert result.unit_ids == (unit.id,)
        assert "terminal persist failed" in result.error
        if outcome == "failed":
            assert "business failure" in result.error
        assert not jobs._futures and not jobs._cancellations
        jobs.cancel(job.id, scope=SCOPE)  # Terminal idempotence, never KeyError.
        if outcome == "succeeded":
            assert jobs.submit(payload_id="p", source_ref="s", scope=SCOPE, task=task).reused
        else:
            with pytest.raises(ConflictError):
                jobs.submit(payload_id="p", source_ref="s", scope=SCOPE, task=task)
    finally:
        release.set()
        jobs.close()


def test_pending_cancel_releases_capacity_and_persists_retry_mapping() -> None:
    kv = InMemoryKVStore()
    jobs = InProcessIngestJobController(max_workers=1, max_pending_jobs=1, kv=kv)
    started, release, unwanted = Event(), Event(), Event()

    def blocked():
        started.set()
        assert release.wait(5), "test must release active worker"
        return []

    try:
        jobs.submit(payload_id="active", source_ref="a", scope=SCOPE, task=blocked)
        assert started.wait(2)
        pending = jobs.submit(
            payload_id="pending", source_ref="b", scope=SCOPE,
            task=lambda: unwanted.set() or [],
        ).job
        with pytest.raises(NotFoundError):
            jobs.cancel(pending.id, scope=Scope(org="acme", user="bob"))
        jobs.cancel(pending.id, scope=SCOPE)
        jobs.cancel(pending.id, scope=SCOPE)
        assert jobs.status(pending.id, scope=SCOPE).status == "cancelled"
        replacement = jobs.submit(
            payload_id="pending", source_ref="c", scope=SCOPE, task=lambda: []
        )
        assert not replacement.reused
        release.set()
        jobs.close()
        assert not unwanted.is_set(), "cancelled queued task must never execute"
        assert not jobs._futures and not jobs._cancellations
        jobs.cancel(pending.id, scope=SCOPE)
        jobs.cancel(replacement.job.id, scope=SCOPE)
    finally:
        release.set()
        jobs.close()
    reader = InProcessIngestJobController(kv=kv)
    try:
        assert reader.status(pending.id, scope=SCOPE).status == "cancelled"
        assert reader.submit(
            payload_id="pending", source_ref="c", scope=SCOPE, task=lambda: []
        ).job.id == replacement.job.id
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("engine_type", "stage"),
    [
        (engine_type, stage)
        for engine_type, stage in product(INGEST_ENGINES, INGEST_STAGES)
        if not (engine_type is AgentEngine and stage == "procedural")
    ],
)
def test_running_ingest_cancellation(engine_type, stage) -> None:
    entered, release = Event(), Event()
    calls = []
    unit = MemoryUnit(id="written", scope=SCOPE, segments=[Segment(content="text")])

    def pause(name):
        calls.append(name)
        if name == stage:
            entered.set()
            assert release.wait(5), "test must release synchronous operation"

    def ingest(_payloads):
        pause("normalize")
        return [unit]

    def build(_units):
        pause("write")

    def evolve(_units, _mode):
        pause(stage)
        return SimpleNamespace(
            created_units=[] if stage == "evolve_read_failure" else [unit],
            created_ids=[unit.id],
        )

    kwargs = dict(
        ingestor=SimpleNamespace(ingest=ingest),
        classifier=SimpleNamespace(classify=lambda units: pause("classify")),
        index_builder=SimpleNamespace(build=build), retriever=None, scheduler=None,
        evolver=SimpleNamespace(evolve=evolve), lifecycle=None, kv=InMemoryKVStore(),
    )
    if engine_type is not InMemoryEngine:
        kwargs["domain_store"] = None
    engine = engine_type(**kwargs)
    if stage == "evolve_read_failure":
        def fail_read(*args):
            raise ValueError("read after write failed")
        engine._load = fail_read
    jobs = InProcessIngestJobController()
    try:
        submission = jobs.submit(
            payload_id="payload", source_ref="source", scope=SCOPE,
            task=lambda: asyncio.run(engine.write(
                "text", SCOPE, system_metadata={
                    "infer": stage in {"evolve", "evolve_read_failure"},
                    "procedural": stage == "procedural",
                }
            )),
        )
        assert entered.wait(2), "ingest must reach the selected operation"
        jobs.cancel(submission.job.id, scope=SCOPE)
        jobs.cancel(submission.job.id, scope=SCOPE)
        running = jobs.status(submission.job.id, scope=SCOPE)
        assert running.status == "running", "cancel request is not execution completion"
        assert running.cancel_requested
        future = jobs._futures[submission.job.id]
        release.set()
        future.result(timeout=3)
        result = jobs.status(submission.job.id, scope=SCOPE)
        assert result.status == ("failed" if stage == "evolve_read_failure" else "cancelled")
        written = stage in {"write", "evolve", "evolve_read_failure", "procedural"}
        assert result.partial_write is written
        assert result.retry_requires_reconciliation is written
        assert result.unit_ids == ((unit.id,) if written else ())
        if not written:
            assert "write" not in calls and "evolve" not in calls
        else:
            with pytest.raises(ConflictError):
                jobs.submit(payload_id="payload", source_ref="source", scope=SCOPE, task=lambda: [])
    finally:
        release.set()
        jobs.close()


@pytest.mark.parametrize("engine_type", [CloudEngine, InMemoryEngine])
def test_cancel_after_middle_write_preserves_ids_without_scheduling(engine_type) -> None:
    entered, release = Event(), Event()
    scheduled = []
    unit = MemoryUnit(id="middle", scope=SCOPE, segments=[Segment(content="text")])

    def build(units):
        entered.set()
        assert release.wait(5)

    async def submit(*args, **kwargs):
        scheduled.append(args)

    kwargs = dict(
        ingestor=SimpleNamespace(ingest=lambda payloads: [unit]),
        index_builder=SimpleNamespace(build=build), retriever=None,
        scheduler=SimpleNamespace(validate=lambda job: None, submit=submit),
        evolver=object(), lifecycle=None, kv=InMemoryKVStore(),
        job_factory=SimpleNamespace(get_job=lambda *args, **kwargs: object()),
    )
    if engine_type is CloudEngine:
        kwargs["domain_store"] = None
    engine = engine_type(**kwargs)
    jobs = InProcessIngestJobController()
    try:
        job = jobs.submit(
            payload_id="middle", source_ref="source", scope=SCOPE,
            task=lambda: asyncio.run(engine.write(
                "text", SCOPE, system_metadata={"infer": True, "middle": True}
            )),
        ).job
        assert entered.wait(2)
        jobs.cancel(job.id, scope=SCOPE)
        release.set()
        jobs.close()
        result = jobs.status(job.id, scope=SCOPE)
        assert result.status == "cancelled"
        assert result.unit_ids == (unit.id,)
        assert result.retry_requires_reconciliation
        assert not scheduled, "cancellation must prevent the next scheduled stage"
    finally:
        release.set()
        jobs.close()


def test_real_failure_is_not_masked_by_cancellation() -> None:
    started, release = Event(), Event()
    jobs = InProcessIngestJobController()

    def fail():
        started.set()
        assert release.wait(5)
        raise ValueError("real failure")

    try:
        job = jobs.submit(payload_id="p", source_ref="s", scope=SCOPE, task=fail).job
        assert started.wait(2)
        jobs.cancel(job.id, scope=SCOPE)
        release.set()
        jobs.close()
        assert jobs.status(job.id, scope=SCOPE).status == "failed"
        assert jobs.status(job.id, scope=SCOPE).error == "real failure"
    finally:
        release.set()
        jobs.close()
