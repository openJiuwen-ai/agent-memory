# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Runtime 周期入口、真实双引擎闭环与生命周期兼容。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.api import assemble_runtime, assemble_server_runtime
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.types import JobStatus
from tests.unit.api.fixtures import HOME, ROOT_SECURITY, security_for
from tests.unit.api.hierarchy_background_fixtures import background_config, wait_for_round

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("policies", [
    {}, {"hierarchy.enabled": "true"}, {"hierarchy.auto_derive": "true"},
])
def test_disabled_registration_skips_even_with_invalid_unused_auto_config(policies) -> None:
    config = background_config()
    config["globals"]["policies"] = policies
    config["job_factory"]["default"]["params"]["hierarchy_derive_interval"] = 0
    config["scheduler"]["default"]["target"] = "in_process"
    runtime = assemble_runtime(config=config)
    try:
        result = asyncio.run(runtime.start_background_jobs(
            HOME, security=security_for(runtime.api, HOME),
        ))
        assert result.skipped and result.reason == "hierarchy_disabled" and not result.job_ids
    finally:
        runtime.close()


def test_enabled_registration_requires_periodic_scheduler() -> None:
    config = background_config()
    config["scheduler"]["default"]["target"] = "in_process"
    runtime = assemble_runtime(config=config)
    try:
        with pytest.raises(ValidationError, match="periodic Scheduler"):
            asyncio.run(runtime.start_background_jobs(
                HOME, security=security_for(runtime.api, HOME),
            ))
    finally:
        runtime.close()


@pytest.mark.parametrize("engine_kind", ["in_memory", "cloud"])
def test_registration_survives_caller_loop_and_records_each_round(engine_kind) -> None:
    runtime = assemble_runtime(config=background_config(engine_kind))
    api = runtime.api
    security = security_for(api, HOME)
    moment = datetime.now(UTC) - timedelta(minutes=2)
    scope = Scope(org=HOME.org, user=HOME.user, agent=HOME.agent, session="live")
    try:
        leaf = api.add("A completed session", scope, security=security,
                       occurred_at=moment, system_metadata={"infer": False})[0]
        started = asyncio.run(runtime.start_background_jobs(HOME, security=security))
        again = asyncio.run(runtime.start_background_jobs(HOME, security=security))
        assert started.job_ids == again.job_ids and not started.skipped
        job_id = started.job_ids[0]
        first = asyncio.run(wait_for_round(api, job_id, security))
        assert first.detail["last_run_status"] == "succeeded", first.detail
        detail = json.loads(first.detail["last_run_detail"])
        assert detail["created_parent_count"] == detail["updated_child_count"] == "1"
        attached = api.get(leaf.id, leaf.scope, security=security)
        parent = api.get(attached.hierarchy.parent_id, HOME, security=security)
        assert parent.hierarchy.child_ids == [leaf.id]
        assert attached.content == leaf.content and attached.temporal == leaf.temporal
        second = asyncio.run(wait_for_round(api, job_id, security, first.detail["last_run_id"]))
        assert json.loads(second.detail["last_run_detail"])["created_parent_count"] == "0"
        api.admin_set("hierarchy.auto_derive", "false", security=ROOT_SECURITY)
        disabled = asyncio.run(wait_for_round(api, job_id, security, second.detail["last_run_id"]))
        assert json.loads(disabled.detail["last_run_detail"])["reason"] == "auto_derive disabled"
        runtime.close()
        assert api.job_status(job_id, security=security).status is JobStatus.CANCELLED
        with pytest.raises(ValidationError, match="closed"):
            asyncio.run(runtime.start_background_jobs(HOME, security=security))
    finally:
        runtime.close()


def test_server_facade_registers_and_public_cancel_stops_timer() -> None:
    runtime = assemble_server_runtime(config=background_config(), security_runtime=None)
    api = runtime.api
    security = security_for(api, HOME)
    try:
        started = asyncio.run(runtime.start_background_jobs(HOME, security=security))
        job_id = started.job_ids[0]
        api.job_cancel(job_id, security=security)
        assert api.job_status(job_id, security=security).status is JobStatus.CANCELLED
        resumed = asyncio.run(runtime.start_background_jobs(HOME, security=security))
        assert resumed.job_ids != started.job_ids
        assert api.job_status(resumed.job_ids[0], security=security).status is JobStatus.RUNNING
    finally:
        runtime.close()


def test_background_home_authorization_is_checked_before_disabled_skip() -> None:
    runtime = assemble_runtime()
    try:
        stranger = security_for(runtime.api, Scope(org=HOME.org, user="stranger"))
        with pytest.raises(PermissionDeniedError):
            asyncio.run(runtime.start_background_jobs(HOME, security=stranger))
    finally:
        runtime.close()


def test_missing_composer_profile_skips_registration_explicitly() -> None:
    config = background_config()
    config["evolver"]["default"]["params"]["hierarchy_composer"] = None
    runtime = assemble_runtime(config=config)
    try:
        result = asyncio.run(runtime.start_background_jobs(
            HOME, security=security_for(runtime.api, HOME),
        ))
        assert result.skipped and result.reason == "hierarchy_profile_missing"
        assert result.job_ids == []
    finally:
        runtime.close()


@pytest.mark.parametrize("enabled", [False, True])
def test_routing_boundary_only_applies_to_enabled_background_jobs(enabled) -> None:
    config = background_config()
    config["globals"]["policies"]["hierarchy.auto_derive"] = str(enabled).lower()
    config["authorizer"] = {
        "default": {"target": "routing", "params": {
            "route_key": "memory_type", "fallback": "strict", "routes": {"note": "strict"},
        }},
        "strict": {"target": "standard", "params": {
            "grant_store": "default", "delegation_store": "default",
        }},
    }
    runtime = assemble_runtime(config=config)
    try:
        security = security_for(runtime.api, HOME)
        if enabled:
            with pytest.raises(ValidationError, match="权限路由"):
                asyncio.run(runtime.start_background_jobs(HOME, security=security))
        else:
            result = asyncio.run(runtime.start_background_jobs(HOME, security=security))
            assert result.skipped and not result.job_ids
    finally:
        runtime.close()
