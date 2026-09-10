# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""公开 API 的四层建树、event 召回、逐层展开及父级上卷。"""

from datetime import timedelta

import pytest

from jiuwen_memory.api import Context, HierarchyRole, JobStatus, assemble_runtime
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory_entry.core.api_contract import invoke_api
from tests.unit.api.hierarchy_api_fixtures import (
    HOME,
    ROOT_SECURITY,
    SECURITY,
    START,
    runtime_config,
    task_options,
    write_snapshot,
)
from tests.unit.api.hierarchy_search_fixtures import hierarchy_search_options

pytestmark = pytest.mark.unit
EVENT_ROLES = [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.EVENT]


@pytest.fixture(name="api", params=["in_memory", "cloud"])
def event_runtime_fixture(request):
    config = runtime_config(request.param)
    profile = config["hierarchy_composer"]["tree"]["params"]["hierarchy_profiles"]["time"]
    profile["parent_roles"] = ["time_span", "scene", "event"]
    profile["stage_options"] = {"SceneSegmenter": {"max_duration_seconds": "1"}}
    runtime = assemble_runtime(config=config)
    try:
        runtime.api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
        yield runtime.api
    finally:
        runtime.close()
        Factory.reset_all()


@pytest.mark.parametrize(("depth", "count"), [(0, 1), (1, 3), (2, 5), (3, 7)])
def test_event_search_expands_each_layer_with_existing_depth_option(api, depth, count) -> None:
    leaves = [write_snapshot(api, minute, session=f"session-{minute}") for minute in (0, 1)]
    job_id = api.evolve(HOME, task_options(parent_roles=list(EVENT_ROLES)), security=SECURITY)
    info = api.job_status(job_id, security=SECURITY)
    assert info.status is JobStatus.SUCCEEDED, info.detail
    assert info.detail["created_parent_count"] == "5"
    result = api.search("snapshot evidence", Context(HOME), hierarchy_search_options(
        hierarchy_role=HierarchyRole.EVENT, expand_depth=depth,
    ), security=SECURITY)
    assert len(result.items) == count
    if depth == 3:
        assert {item.unit_id for item in result.items[5:]} == {leaf.id for leaf in leaves}
        assert {item.content for item in result.items[5:]} == {leaf.content for leaf in leaves}
    assert not result.errors


def test_event_rollup_and_depth_three_share_search_protocol(api) -> None:
    leaves = [write_snapshot(api, minute, session=f"session-{minute}") for minute in (0, 1)]
    job_id = api.evolve(HOME, task_options(parent_roles=list(EVENT_ROLES)), security=SECURITY)
    assert api.job_status(job_id, security=SECURITY).status is JobStatus.SUCCEEDED
    result = invoke_api(api, "search", {
        "query": "snapshot evidence",
        "context": {"scope": {"org": HOME.org, "user": HOME.user, "agent": HOME.agent}},
        "options": {"hierarchy_kind": "time", "hierarchy_role": "event",
                    "rollup": True, "expand_depth": 3, "with_trajectory": True},
    }, SECURITY)
    assert len(result["items"]) == 7
    assert {item["unit_id"] for item in result["items"][5:]} == {leaf.id for leaf in leaves}
    assert any(step["stage"] == "rollup" for step in result["trajectory"])
    assert result["errors"] == []


def test_public_event_gap_rebuild_keeps_every_original_descendant(api) -> None:
    leaves = [write_snapshot(api, minute, session=f"session-{minute}") for minute in (0, 30)]
    initial = api.evolve(HOME, task_options(
        parent_roles=list(EVENT_ROLES), span_end=START + timedelta(minutes=30),
    ), security=SECURITY)
    assert api.job_status(initial, security=SECURITY).status is JobStatus.SUCCEEDED
    rebuilt = api.evolve(HOME, task_options(
        parent_roles=list(EVENT_ROLES), span_start=START + timedelta(minutes=15),
        span_end=START + timedelta(minutes=15),
    ), security=SECURITY)
    info = api.job_status(rebuilt, security=SECURITY)
    assert info.status is JobStatus.SUCCEEDED, info.detail
    assert info.detail["replaced_parent_count"] == "5"
    assert info.detail["updated_child_count"] == "2"
    result = api.search("snapshot evidence", Context(HOME), hierarchy_search_options(
        hierarchy_role=HierarchyRole.EVENT, expand_depth=3, span_end=START + timedelta(minutes=30),
    ), security=SECURITY)
    assert len(result.items) == 7
    assert {item.unit_id for item in result.items[5:]} == {leaf.id for leaf in leaves}
    assert not result.errors
