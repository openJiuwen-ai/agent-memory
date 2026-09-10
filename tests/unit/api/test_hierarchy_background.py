# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真实 Runtime 的 opt-in、PEP、长驻异步调度与关闭边界。"""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.api import JobStatus, assemble_runtime
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.legacy import legacy_request_context
from tests.unit.api.hierarchy_api_fixtures import HOME, ROOT_SECURITY, SECURITY, runtime_config

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("engine", ["in_memory", "cloud"])
def test_runtime_default_off_and_nonperiodic_scheduler_fails_fast(engine) -> None:
    runtime = assemble_runtime(config=runtime_config(engine))
    try:
        assert asyncio.run(runtime.start_background_jobs(HOME, security=SECURITY)) == []
        runtime.api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
        assert asyncio.run(runtime.start_background_jobs(HOME, security=SECURITY)) == []
        runtime.api.admin_set("hierarchy.auto_derive", "true", security=ROOT_SECURITY)
        with pytest.raises(ValidationError, match="periodic Scheduler"):
            asyncio.run(runtime.start_background_jobs(HOME, security=SECURITY))
    finally:
        runtime.close()


@pytest.mark.parametrize("engine", ["in_memory", "cloud"])
def test_live_runtime_runs_once_then_noop_and_close_cancels_future_rounds(engine) -> None:
    config = runtime_config(engine)
    config["scheduler"] = {"default": {"target": "async_timer", "params": {"tick_interval": 1}}}
    config["job_factory"] = {"default": {"target": "default", "params": {
        "hierarchy_derive_interval": 1,
    }}}
    profile = config["hierarchy_composer"]["tree"]["params"]["hierarchy_profiles"]["time"]
    profile["stage_options"] = {"TimeSpanMerger": {"gap_seconds": "1"}}
    runtime = assemble_runtime(config=config)
    api = runtime.api
    point = datetime.now(timezone.utc) - timedelta(minutes=2)
    leaf_scope = replace(HOME, session="live")
    leaf = api.add("live evidence", leaf_scope, security=SECURITY, system_metadata={
        "hierarchy_kind": "time", "hierarchy_role": "snapshot", "infer": False,
        "hierarchy_span_start": point.isoformat(), "hierarchy_span_end": point.isoformat(),
    })[0]
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    api.admin_set("hierarchy.auto_derive", "true", security=ROOT_SECURITY)

    async def exercise() -> None:
        ids = await runtime.start_background_jobs(HOME, security=SECURITY)
        again = await runtime.start_background_jobs(HOME, security=SECURITY)
        if ids != again or len(ids) != 1:
            pytest.fail(f"periodic registration must coalesce, got {ids!r} and {again!r}")
        timer_id = ids[0]
        async with asyncio.timeout(5):
            while "last_run_id" not in api.job_status(timer_id, security=SECURITY).detail:
                await asyncio.sleep(0.02)
        first = api.job_status(timer_id, security=SECURITY)
        first_detail = json.loads(first.detail["last_run_detail"])
        if first.detail["last_run_status"] != "succeeded":
            pytest.fail(f"periodic round did not succeed: {first.detail}")
        if first_detail["created_parent_count"] != "1":
            pytest.fail(f"expected one parent from live round: {first_detail}")
        first_run = first.detail["last_run_id"]
        async with asyncio.timeout(5):
            while api.job_status(timer_id, security=SECURITY).detail["last_run_id"] == first_run:
                await asyncio.sleep(0.02)
        second = api.job_status(timer_id, security=SECURITY)
        if json.loads(second.detail["last_run_detail"])["created_parent_count"] != "0":
            pytest.fail(f"idle round rebuilt the tree: {second.detail}")
        runtime.close()
        if api.job_status(timer_id, security=SECURITY).status is not JobStatus.CANCELLED:
            pytest.fail("runtime.close did not cancel its periodic registration")
        with pytest.raises(ValidationError, match="closed"):
            await runtime.start_background_jobs(HOME, security=SECURITY)

    try:
        asyncio.run(exercise())
        stored = api.get(leaf.id, leaf_scope, security=SECURITY)
        assert stored.hierarchy.parent_id
        assert stored.hierarchy.parent_scope == HOME
        assert stored.content == leaf.content
    finally:
        runtime.close()


def test_runtime_rejects_unauthorized_home_even_before_enablement() -> None:
    runtime = assemble_runtime(config=runtime_config("in_memory"))
    other = legacy_request_context(replace(HOME, user="mallory", agent="other"))
    try:
        with pytest.raises(PermissionDeniedError):
            asyncio.run(runtime.start_background_jobs(HOME, security=other))
    finally:
        runtime.close()


def test_runtime_refuses_permission_routing_before_submission() -> None:
    config = runtime_config("in_memory")
    config["permission"] = {
        "default": {"target": "routing", "params": {
            "route_key": "memory_type", "fallback": "strict", "routes": {"private": "strict"},
        }},
        "strict": "sqlite",
    }
    runtime = assemble_runtime(config=config)
    try:
        with pytest.raises(ValidationError, match="权限路由"):
            asyncio.run(runtime.start_background_jobs(HOME, security=SECURITY))
    finally:
        runtime.close()
