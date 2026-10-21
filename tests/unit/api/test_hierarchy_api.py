# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""公开 API 的构建、完整重建、失败报告与第一阶段接入兼容。"""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from jiuwen_memory.api import EvolveMode, EvolveTaskOptions, assemble_runtime
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, LifecycleState, Scope
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.control.types import JobStatus
from jiuwen_memory.storage.types import IndexWriteMode
from tests.unit.api.fixtures import HOME, ROOT_SECURITY, START, security_for
from tests.unit.api.hierarchy_api_fixtures import runtime_config, task_options, write_snapshot

pytestmark = pytest.mark.unit


@pytest.fixture(name="api", params=["in_memory", "cloud"])
def hierarchy_runtime_fixture(request):
    runtime = assemble_runtime(config=runtime_config(request.param))
    try:
        yield runtime.api
    finally:
        runtime.close()


def test_explicit_api_build_and_partial_span_rebuild_preserve_all_old_children(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    leaves = [write_snapshot(api, minute) for minute in (0, 1)]
    original = deepcopy(leaves)
    requested = task_options(metadata={"build_source": "explicit-test"})
    original_options = deepcopy(requested)

    job_id = api.evolve_v2(HOME, requested, security=security_for(api, HOME))
    first = api.job_status(job_id, security=security_for(api, HOME))
    attached = [api.get(leaf.id, leaf.scope, security=security_for(api, HOME)) for leaf in leaves]
    parent_id = attached[0].hierarchy.parent_id
    parent = api.get(parent_id, HOME, security=security_for(api, HOME))

    assert first.status is JobStatus.SUCCEEDED and first.mode == "hierarchy"
    assert first.detail["created_parent_count"] == "1"
    assert first.detail["updated_child_count"] == "2"
    assert requested == original_options, "公开入口不修改调用方的请求对象"
    assert parent.hierarchy.role is HierarchyRole.TIME_SPAN
    assert set(parent.hierarchy.child_ids) == {leaf.id for leaf in leaves}
    assert parent.system_metadata["build_source"] == "explicit-test"
    for leaf in attached:
        assert leaf.hierarchy.parent_id == parent_id
        assert leaf.hierarchy.parent_scope == HOME

    # 没有新增叶，窗口只覆盖第一条；仍补齐旧父的第二条子叶再重建。
    second_id = api.evolve_v2(HOME, task_options(span_end=START), security=security_for(api, HOME))
    second = api.job_status(second_id, security=security_for(api, HOME))
    retired = api.get(parent_id, HOME, security=security_for(api, HOME))
    rebuilt = [api.get(leaf.id, leaf.scope, security=security_for(api, HOME)) for leaf in leaves]

    assert second.status is JobStatus.SUCCEEDED
    assert second.detail["replaced_parent_count"] == "1"
    assert second.detail["updated_child_count"] == "2"
    assert retired.lifecycle is LifecycleState.ARCHIVED
    assert retired.hierarchy.child_ids == []
    assert rebuilt[0].hierarchy.parent_id == rebuilt[1].hierarchy.parent_id != parent_id
    replacement = api.get(rebuilt[0].hierarchy.parent_id, HOME, security=security_for(api, HOME))
    assert set(replacement.hierarchy.child_ids) == {leaf.id for leaf in leaves}
    for before, after in zip(original, rebuilt):
        assert after.content == before.content
        assert after.temporal == before.temporal and after.provenance == before.provenance
        assert after.lifecycle == before.lifecycle and after.user_metadata == before.user_metadata


def test_one_home_task_can_build_separate_parents_for_multiple_sessions(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    first = write_snapshot(api, 0, "first-session")
    second = write_snapshot(api, 1, "second-session")
    job_id = api.evolve_v2(HOME, task_options(), security=security_for(api, HOME))
    status = api.job_status(job_id, security=security_for(api, HOME))
    first_stored = api.get(first.id, first.scope, security=security_for(api, HOME))
    second_stored = api.get(second.id, second.scope, security=security_for(api, HOME))
    assert status.status is JobStatus.SUCCEEDED
    assert status.detail["created_parent_count"] == "2"
    assert first_stored.hierarchy.parent_id != second_stored.hierarchy.parent_id
    assert first_stored.hierarchy.parent_scope == second_stored.hierarchy.parent_scope == HOME


@pytest.mark.parametrize("overrides", [
    {"tree_home_scope": Scope(org="another-org")},
    {"kind": HierarchyKind.TOPIC},
    {"leaf_role": HierarchyRole.SCENE},
    {"parent_roles": [HierarchyRole.TIME_SPAN, HierarchyRole.EVENT]},
    {"span_start": None},
    {"span_end": START - timedelta(seconds=1)},
    {"metadata": {"bad": 42}},
])
def test_invalid_hierarchy_options_are_rejected_before_submission(api, overrides) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    with pytest.raises(ValidationError):
        api.evolve_v2(HOME, task_options(**overrides), security=security_for(api, HOME))


@pytest.mark.parametrize("key", ["author_principal", "memory_class", "tree_parent_id"])
def test_parent_metadata_cannot_forge_kernel_fields(api, key) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    with pytest.raises(ValidationError, match="内核保留 key"):
        api.evolve_v2(HOME, task_options(metadata={key: "forged"}),
                      security=security_for(api, HOME))


def test_non_hierarchy_mode_cannot_silently_ignore_hierarchy_options(api) -> None:
    options = EvolveTaskOptions(
        mode=EvolveMode.EXTRACT, hierarchy_options=task_options().hierarchy_options,
    )
    with pytest.raises(ValidationError):
        api.evolve_v2(HOME, options, security=security_for(api, HOME))


def test_no_candidates_is_an_explicit_successful_noop(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    job_id = api.evolve_v2(HOME, task_options(), security=security_for(api, HOME))
    status = api.job_status(job_id, security=security_for(api, HOME))
    assert status.status is JobStatus.SUCCEEDED
    assert status.detail["reason"] == "no candidates"
    assert status.detail["created_parent_count"] == "0"


def test_api_reports_failed_when_parent_index_write_requires_repair(api, monkeypatch) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    leaf = write_snapshot(api, 0)
    original_build = HybridIndexBuilder.build

    def fail_parent_retrieval_index(builder, units, *, mode=IndexWriteMode.ALL) -> None:
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            raise RuntimeError("injected parent retrieval index failure")
        original_build(builder, units, mode=mode)

    monkeypatch.setattr(HybridIndexBuilder, "build", fail_parent_retrieval_index)
    job_id = api.evolve_v2(HOME, task_options(), security=security_for(api, HOME))
    status = api.job_status(job_id, security=security_for(api, HOME))
    stored = api.get(leaf.id, leaf.scope, security=security_for(api, HOME))
    parent = api.get(stored.hierarchy.parent_id, HOME, security=security_for(api, HOME))

    assert status.status is JobStatus.FAILED
    assert status.detail["complete"] == "false"
    assert int(status.detail["repair_required_count"]) > 0
    assert "index_build_failed" in status.detail["repair_required"]
    assert parent.hierarchy.child_ids == [leaf.id], "失败不宣称回滚已写入的本体与父子边"


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("engine_kind", ["in_memory", "cloud"])
def test_build_reuse_and_explicit_mixed_rebuild(engine_kind, depth):
    runtime = assemble_runtime(config=runtime_config(engine_kind, depth))
    api = runtime.api
    try:
        leaves = [write_snapshot(api, 0, "a"), write_snapshot(api, 1, "b")]
        options = task_options(depth)
        options = replace(options, reuse_existing=True)
        security = security_for(api, HOME)
        first = api.job_status(api.evolve_v2(HOME, options, security=security), security=security)
        assert first.status is JobStatus.SUCCEEDED, first.detail
        assert int(first.detail["created_parent_count"]) == depth + 1
        reused = api.job_status(api.evolve_v2(HOME, options, security=security), security=security)
        assert reused.status is JobStatus.SUCCEEDED, reused.detail
        assert reused.detail["reused"] == "true" and reused.detail["built"] == "false"
        assert reused.detail["created_parent_count"] == "0"
        leaves.append(write_snapshot(api, 1, "c"))
        rejected_id = api.evolve_v2(HOME, options, security=security)
        rejected = api.job_status(rejected_id, security=security)
        assert rejected.status is JobStatus.FAILED
        assert "混合" in rejected.detail["error"]
        options = replace(options, reuse_existing=False)
        rebuilt = api.job_status(api.evolve_v2(HOME, options, security=security), security=security)
        assert rebuilt.status is JobStatus.SUCCEEDED, rebuilt.detail
        assert int(rebuilt.detail["replaced_parent_count"]) == depth + 1
        for leaf in leaves:
            stored = api.get(leaf.id, leaf.scope, security=security)
            assert stored.content == leaf.content and stored.temporal == leaf.temporal
            assert stored.hierarchy.parent_id
    finally:
        runtime.close()


@pytest.mark.parametrize("with_valid", [False, True])
def test_incomplete_time_is_preserved_and_only_skipped_during_build(api, with_valid):
    security = security_for(api, HOME)
    leaves = [api.add("no timestamp", HOME, security=security,
                      system_metadata={"infer": False})[0]]
    leaves.append(api.add("partial time", HOME, security=security, occurred_at=START,
                          system_metadata={"infer": False, "hierarchy_kind": "time",
                                           "hierarchy_role": "snapshot",
                                           "hierarchy_span_start": START.isoformat()})[0])
    eligible = write_snapshot(api, 0) if with_valid else None
    options = task_options()
    options = replace(options, reuse_existing=True)
    info = api.job_status(api.evolve_v2(HOME, options, security=security), security=security)
    assert info.status is JobStatus.SUCCEEDED, info.detail
    assert info.detail["skipped_incomplete_time_count"] == "2"
    assert info.detail["created_parent_count"] == ("1" if with_valid else "0")
    for leaf in leaves:
        assert api.get(leaf.id, leaf.scope, security=security) == leaf
    if eligible:
        assert api.get(eligible.id, eligible.scope, security=security).hierarchy.parent_id
    else:
        assert info.detail["reason"] == "no_eligible_candidates"


@pytest.mark.parametrize("policies", [{"hierarchy.enabled": "false"}, {"infer.default": "false"}])
def test_disabled_or_missing_switch_skips_before_optional_model_resolution(policies):
    config = runtime_config("in_memory")
    config["globals"] = {"policies": policies, "vector_enabled": False,
                         "graph_enabled": False, "layers_index_enabled": False,
                         "entity_enabled": False}
    config["constructor"] = {"default": {"target": "unified"}}
    config["dedup"] = {"default": {"target": "keyword"}}
    config["embedder"] = {"default": {"target": "not_installed_optional_embedder"}}
    del config["evolver"]["default"]["params"]["hierarchy_composer"]
    del config["hierarchy_composer"]
    runtime = assemble_runtime(config=config)
    try:
        api = runtime.api
        security = security_for(api, HOME)
        options = task_options()
        info = api.job_status(api.evolve_v2(HOME, options, security=security), security=security)
        assert info.status is JobStatus.SUCCEEDED
        assert info.detail["reason"] == "hierarchy_disabled"
        with pytest.raises(ValidationError):
            api.evolve_v2(HOME, task_options(span_start=None), security=security)
        if "hierarchy.enabled" not in policies:
            return
        api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
        write_snapshot(api, 0, "a")
        write_snapshot(api, 1, "b")
        failed = api.job_status(api.evolve_v2(HOME, task_options(3), security=security),
                                security=security)
        assert failed.status is JobStatus.FAILED
        assert "not_installed_optional_embedder" in failed.detail["error"]
    finally:
        runtime.close()


def test_normal_v2_keeps_legacy_candidate_and_dreaming_arguments(api, monkeypatch):
    from unittest.mock import Mock

    legacy = Mock(return_value="existing-job")
    monkeypatch.setattr(api, "evolve", legacy)
    security = security_for(api, HOME)
    options = EvolveTaskOptions(EvolveMode.EXTRACT, candidate={"source": "custom"},
                                dreaming=True, interval=60)
    assert api.evolve_v2(HOME, options, security=security) == "existing-job"
    legacy.assert_called_once_with(HOME, EvolveMode.EXTRACT, options.channel,
                                   candidate=options.candidate, dreaming=True,
                                   interval=60, security=security)


def test_hierarchy_uses_current_authorizer_routing_boundary():
    config = runtime_config("in_memory")
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
        with pytest.raises(ValidationError, match="权限路由"):
            runtime.api.evolve_v2(HOME, task_options(), security=security_for(runtime.api, HOME))
    finally:
        runtime.close()


def test_write_grant_without_update_cannot_reparent(api):
    from jiuwen_memory.api import Action, Grant, PermissionDeniedError

    visitor = Scope(org=HOME.org, user="visitor")
    api.grant(Grant(grantor=HOME, grantee=visitor, actions=[Action.WRITE]),
              security=ROOT_SECURITY)
    with pytest.raises(PermissionDeniedError):
        api.evolve_v2(HOME, task_options(), security=security_for(api, visitor))


@pytest.mark.parametrize("scheduler_kind", ["in_process", "async_timer"])
def test_builtin_four_layer_request_and_job_status(scheduler_kind):
    import time

    config = runtime_config("in_memory")
    del config["hierarchy_composer"]
    del config["evolver"]["default"]["params"]["hierarchy_composer"]
    config["scheduler"] = {"default": {"target": scheduler_kind}}
    runtime = assemble_runtime(config=config)
    try:
        api = runtime.api
        security = security_for(api, HOME)
        leaves = [write_snapshot(api, minute, str(minute)) for minute in (0, 1)]
        options = EvolveTaskOptions.time_hierarchy(
            HOME, span_start=START, span_end=START + timedelta(minutes=1), reuse_existing=True,
        )
        job_id = api.evolve_v2(HOME, options, security=security)
        deadline = time.monotonic() + 5
        while True:
            info = api.job_status(job_id, security=security)
            if info.status not in (JobStatus.PENDING, JobStatus.RUNNING):
                break
            assert time.monotonic() < deadline, "background construction did not finish"
            time.sleep(0.01)
        assert info.status is JobStatus.SUCCEEDED, info.detail
        assert info.detail["created_parent_count"] == "4"
        for leaf in leaves:
            stored = api.get(leaf.id, leaf.scope, security=security)
            assert stored.hierarchy.parent_id and stored.temporal == leaf.temporal
    finally:
        runtime.close()
