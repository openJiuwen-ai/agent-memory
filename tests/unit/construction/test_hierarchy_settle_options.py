# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""独立静默参数的解析、向后兼容和显式建树隔离。"""

import asyncio
from copy import deepcopy

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole
from jiuwen_memory.construction.hierarchy_composer_impl.scene_pipeline import SceneSegmenterOptions
from jiuwen_memory.construction.hierarchy_composer_impl.time_pipeline import TimeSpanMergerOptions
from jiuwen_memory.control.jobs_impl.hierarchy_job import HierarchyJob, HierarchyJobDependencies
from jiuwen_memory.control.types import JobStatus
from tests.unit.construction.event_fixtures import EVENT_ROLES, event_profiles, read_event_path
from tests.unit.construction.hierarchy_fixtures import make_leaf
from tests.unit.control.hierarchy_job_fixtures import job_harness

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("options_class", [TimeSpanMergerOptions, SceneSegmenterOptions])
def test_optional_settle_uses_legacy_fallback_when_absent(options_class) -> None:
    assert options_class.from_stage_options({}).settle_seconds is None
    assert options_class.from_stage_options({"settle_seconds": ""}).settle_seconds is None
    assert options_class.from_stage_options({"settle_seconds": " 60 "}).settle_seconds == 60


@pytest.mark.parametrize("options_class", [TimeSpanMergerOptions, SceneSegmenterOptions])
@pytest.mark.parametrize("invalid", [True, False, 0, -1, 1.5, "60"])
def test_direct_settle_rejects_nonpositive_or_noninteger(options_class, invalid) -> None:
    with pytest.raises(ValidationError):
        options_class(settle_seconds=invalid)


def test_explicit_historical_build_and_reuse_ignore_all_quiet_thresholds() -> None:
    leaves = [make_leaf("first"), make_leaf("second", 1)]
    baseline = None
    for quiet in (None, "1", "1000000000"):
        profiles = event_profiles()
        if quiet is not None:
            for stage in profiles[HierarchyKind.TIME].stage_options.values():
                stage["settle_seconds"] = quiet
        harness = job_harness(deepcopy(leaves), profiles=profiles)
        harness.options.parent_roles = list(EVENT_ROLES)
        result = asyncio.run(harness.job().run())
        assert result.status is JobStatus.SUCCEEDED, result.detail
        paths = [read_event_path(harness, leaf) for leaf in leaves]
        shape = [[(node.hierarchy.role, node.hierarchy.span_start, node.hierarchy.span_end,
                   node.content, len(node.hierarchy.child_ids)) for node in path] for path in paths]
        if baseline is None:
            baseline = shape
        else:
            assert shape == baseline, "静默设置不能改变显式历史区间建树的分组或正文"
        assert paths[0][-1].hierarchy.role is HierarchyRole.EVENT
        harness.composition.builder.calls.clear()
        reuse_job = HierarchyJob(
            harness.home, HierarchyJobDependencies(harness.kv, harness.evolver),
            harness.options, reuse_existing=True,
        )
        reused = asyncio.run(reuse_job.run())
        assert reused.status is JobStatus.SUCCEEDED, reused.detail
        assert not harness.composition.builder.calls
