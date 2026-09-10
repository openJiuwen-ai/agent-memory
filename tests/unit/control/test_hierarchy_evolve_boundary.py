# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""阶段二不开放任务建树：真实 API/Engine 必须在 Scheduler.submit 前拒绝。"""

import asyncio
from dataclasses import dataclass

import pytest

from jiuwen_memory.api import MemoryAPI, assemble_runtime
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.construction.evolver import EvolveMode
from jiuwen_memory.control.engine import EngineProducer, MemoryEngine
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.scheduler import SchedulerProducer
from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler
from jiuwen_memory.control.types import Channel, JobStatus

pytestmark = pytest.mark.unit

_SCOPE = Scope(user="alice", agent="assistant")
_SECURITY = legacy_request_context(_SCOPE)


class BoundaryRecordingScheduler(InProcessScheduler):
    """在公开调度入口记录调用，普通演进仍委托真实 Scheduler。"""

    def __init__(self) -> None:
        super().__init__()
        self.submissions: list[tuple[Job, Channel]] = []

    async def submit(self, job: Job, channel: Channel) -> str:
        self.submissions.append((job, channel))
        return await super().submit(job, channel)


@dataclass
class HierarchyBoundaryHarness:
    api: MemoryAPI
    engine: MemoryEngine
    scheduler: BoundaryRecordingScheduler


@pytest.fixture(name="boundary", params=["in_memory", "cloud"], ids=["local", "cloud"])
def hierarchy_boundary_fixture(request, monkeypatch):
    scheduler = BoundaryRecordingScheduler()

    def build_scheduler(scheduler_name, _assembly_context):
        if scheduler_name != "default":
            pytest.fail(f"unexpected scheduler reference: {scheduler_name}")
        return scheduler

    monkeypatch.setattr(SchedulerProducer, "build_named", build_scheduler)
    components = ("ingestor", "index_builder", "retriever", "scheduler", "evolver", "lifecycle")
    config = {
        "engine": {"default": {
            "target": request.param, "params": {name: "default" for name in components},
        }},
        "security": {"default": {"target": "local", "params": {"key_hex": "0" * 64}}},
    }
    runtime = assemble_runtime(config=config)
    try:
        context = default_context().merged(AssemblyContext.from_dict(config))
        engine = EngineProducer.build_named("default", context)
        yield HierarchyBoundaryHarness(runtime.api, engine, scheduler)
    finally:
        runtime.close()
        Factory.reset_all()


@pytest.mark.parametrize("channel", [Channel.HOT, Channel.BACKGROUND])
def test_engine_rejects_hierarchy_before_scheduler_submission(boundary, channel) -> None:
    with pytest.raises(ValidationError, match="HIERARCHY.*任务入口尚未开放"):
        asyncio.run(boundary.engine.evolve(_SCOPE, EvolveMode.HIERARCHY, channel))

    assert boundary.scheduler.submissions == []


@pytest.mark.parametrize("channel", [Channel.HOT, Channel.BACKGROUND])
def test_api_rejects_hierarchy_before_scheduler_submission(boundary, channel) -> None:
    with pytest.raises(ValidationError, match="HIERARCHY.*公开入口尚未开放"):
        boundary.api.evolve(_SCOPE, EvolveMode.HIERARCHY, channel, security=_SECURITY)

    assert boundary.scheduler.submissions == []


def test_api_non_hierarchy_mode_still_uses_same_scheduler(boundary) -> None:
    job_id = boundary.api.evolve(_SCOPE, EvolveMode.FORGET, Channel.HOT, security=_SECURITY)

    assert len(boundary.scheduler.submissions) == 1
    submitted, channel = boundary.scheduler.submissions[0]
    assert submitted.scope == _SCOPE
    assert submitted.mode == EvolveMode.FORGET.value
    assert channel is Channel.HOT
    status = boundary.api.job_status(job_id, security=_SECURITY)
    assert status.status is JobStatus.SUCCEEDED
