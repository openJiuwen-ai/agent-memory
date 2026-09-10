# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""公开 API 的三层建树、scene 召回、深度 2 原文展开及 time_span 上卷。"""

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


@pytest.fixture(name="api", params=["in_memory", "cloud"])
def scene_runtime_fixture(request):
    config = runtime_config(request.param)
    profile = config["hierarchy_composer"]["tree"]["params"]["hierarchy_profiles"]["time"]
    profile["parent_roles"] = ["time_span", "scene"]
    runtime = assemble_runtime(config=config)
    try:
        runtime.api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
        yield runtime.api
    finally:
        runtime.close()
        Factory.reset_all()


def test_built_scene_can_expand_to_original_evidence_in_two_steps(api) -> None:
    leaves = [write_snapshot(api, minute, session=f"session-{minute}") for minute in (0, 1)]
    job_id = api.evolve(HOME, task_options(
        parent_roles=[HierarchyRole.TIME_SPAN, HierarchyRole.SCENE],
    ), security=SECURITY)
    info = api.job_status(job_id, security=SECURITY)
    assert info.status is JobStatus.SUCCEEDED, info.detail
    assert info.detail["created_parent_count"] == "3"
    roots = api.search("snapshot evidence", Context(HOME), hierarchy_search_options(
        hierarchy_role=HierarchyRole.SCENE,
    ), security=SECURITY)
    assert len(roots.items) == 1
    assert roots.items[0].unit_id not in {leaf.id for leaf in leaves}
    assert not roots.errors
    result = api.search("snapshot evidence", Context(HOME), hierarchy_search_options(
        hierarchy_role=HierarchyRole.SCENE, expand_depth=2,
    ), security=SECURITY)
    assert len(result.items) == 5
    assert result.items[0].unit_id == roots.items[0].unit_id
    assert {item.unit_id for item in result.items[3:]} == {leaf.id for leaf in leaves}
    assert {item.content for item in result.items[3:]} == {leaf.content for leaf in leaves}
    assert not result.errors


def test_scene_rollup_and_expansion_use_existing_search_options(api) -> None:
    leaves = [write_snapshot(api, minute, session=f"session-{minute}") for minute in (0, 1)]
    job_id = api.evolve(HOME, task_options(
        parent_roles=[HierarchyRole.TIME_SPAN, HierarchyRole.SCENE],
    ), security=SECURITY)
    assert api.job_status(job_id, security=SECURITY).status is JobStatus.SUCCEEDED
    result = invoke_api(api, "search", {
        "query": "snapshot evidence",
        "context": {"scope": {"org": HOME.org, "user": HOME.user, "agent": HOME.agent}},
        "options": {"hierarchy_kind": "time", "hierarchy_role": "scene",
                    "rollup": True, "expand_depth": 2, "with_trajectory": True},
    }, SECURITY)
    assert len(result["items"]) == 5
    assert {item["unit_id"] for item in result["items"][3:]} == {leaf.id for leaf in leaves}
    assert any(step["stage"] == "rollup" for step in result["trajectory"])
    assert result["errors"] == []


def test_public_rebuild_in_scene_gap_keeps_all_descendants(api) -> None:
    leaves = [write_snapshot(api, minute, session=f"session-{minute}") for minute in (0, 30)]
    initial = api.evolve(HOME, task_options(
        parent_roles=[HierarchyRole.TIME_SPAN, HierarchyRole.SCENE],
        span_end=START + timedelta(minutes=30),
    ), security=SECURITY)
    assert api.job_status(initial, security=SECURITY).status is JobStatus.SUCCEEDED
    rebuilt = api.evolve(HOME, task_options(
        parent_roles=[HierarchyRole.TIME_SPAN, HierarchyRole.SCENE],
        span_start=START + timedelta(minutes=15), span_end=START + timedelta(minutes=15),
    ), security=SECURITY)
    info = api.job_status(rebuilt, security=SECURITY)
    assert info.status is JobStatus.SUCCEEDED, info.detail
    assert info.detail["replaced_parent_count"] == "3"
    assert info.detail["updated_child_count"] == "2"
    result = api.search("snapshot evidence", Context(HOME), hierarchy_search_options(
        hierarchy_role=HierarchyRole.SCENE, span_end=START + timedelta(minutes=30), expand_depth=2,
    ), security=SECURITY)
    assert {item.unit_id for item in result.items[3:]} == {leaf.id for leaf in leaves}
    assert not result.errors
