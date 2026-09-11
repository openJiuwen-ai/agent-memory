# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""API → Engine → Job → Evolver → Composer 的显式建树链路与配置模板。"""

from copy import deepcopy
from datetime import timedelta

import pytest

from jiuwen_memory.api import EvolveMode, EvolveTaskOptions, assemble_runtime
from jiuwen_memory.common.errors import PermissionDeniedError, PolicyError, ValidationError
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.security.types import Action, Grant
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, LifecycleState, Scope
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.control.types import JobStatus
from jiuwen_memory.storage.types import IndexWriteMode
from tests.unit.api.hierarchy_api_fixtures import (
    HOME,
    ROOT_SECURITY,
    SECURITY,
    START,
    hierarchy_template_config,
    runtime_config,
    task_options,
    write_snapshot,
)

pytestmark = pytest.mark.unit


def test_config_template_supports_explicit_four_level_construction() -> None:
    runtime = assemble_runtime(config=hierarchy_template_config())
    try:
        leaves = [
            write_snapshot(runtime.api, minute, session=f"session-{minute}") for minute in (0, 1)
        ]
        options = task_options(parent_roles=[
            HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.EVENT,
        ])
        job_id = runtime.api.evolve(HOME, options, security=SECURITY)
        status = runtime.api.job_status(job_id, security=SECURITY)
        parents = runtime.api.list(HOME, security=SECURITY).items
        roles = [parent.hierarchy.role for parent in parents]
        assert status.status is JobStatus.SUCCEEDED, status.detail
        assert status.detail["created_parent_count"] == "4"
        assert roles.count(HierarchyRole.TIME_SPAN) == 2
        assert roles.count(HierarchyRole.SCENE) == 1
        assert roles.count(HierarchyRole.EVENT) == 1
        for leaf in leaves:
            stored = runtime.api.get(leaf.id, leaf.scope, security=SECURITY)
            assert stored.hierarchy.parent_id
            assert stored.content == leaf.content
    finally:
        runtime.close()


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

    job_id = api.evolve(HOME, requested, security=SECURITY)
    first = api.job_status(job_id, security=SECURITY)
    attached = [api.get(leaf.id, leaf.scope, security=SECURITY) for leaf in leaves]
    parent_id = attached[0].hierarchy.parent_id
    parent = api.get(parent_id, HOME, security=SECURITY)

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
    second_id = api.evolve(HOME, task_options(span_end=START), security=SECURITY)
    second = api.job_status(second_id, security=SECURITY)
    retired = api.get(parent_id, HOME, security=SECURITY)
    rebuilt = [api.get(leaf.id, leaf.scope, security=SECURITY) for leaf in leaves]

    assert second.status is JobStatus.SUCCEEDED
    assert second.detail["replaced_parent_count"] == "1"
    assert second.detail["updated_child_count"] == "2"
    assert retired.lifecycle is LifecycleState.ARCHIVED
    assert retired.hierarchy.child_ids == []
    assert rebuilt[0].hierarchy.parent_id == rebuilt[1].hierarchy.parent_id != parent_id
    replacement = api.get(rebuilt[0].hierarchy.parent_id, HOME, security=SECURITY)
    assert set(replacement.hierarchy.child_ids) == {leaf.id for leaf in leaves}
    for before, after in zip(original, rebuilt):
        assert after.content == before.content
        assert after.temporal == before.temporal and after.provenance == before.provenance
        assert after.lifecycle == before.lifecycle and after.user_metadata == before.user_metadata


def test_hierarchy_policy_is_disabled_by_default_before_submission(api) -> None:
    leaf = write_snapshot(api, 0)
    with pytest.raises(PolicyError, match="hierarchy.enabled=false"):
        api.evolve(HOME, task_options(), security=SECURITY)
    stored = api.get(leaf.id, leaf.scope, security=SECURITY)
    assert stored.hierarchy.parent_id == ""


def test_one_home_task_can_build_separate_parents_for_multiple_sessions(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    first = write_snapshot(api, 0, "first-session")
    second = write_snapshot(api, 1, "second-session")
    job_id = api.evolve(HOME, task_options(), security=SECURITY)
    status = api.job_status(job_id, security=SECURITY)
    first_stored = api.get(first.id, first.scope, security=SECURITY)
    second_stored = api.get(second.id, second.scope, security=SECURITY)
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
        api.evolve(HOME, task_options(**overrides), security=SECURITY)


@pytest.mark.parametrize("key", ["author_principal", "memory_class", "parent_id"])
def test_parent_metadata_cannot_forge_kernel_fields(api, key) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    with pytest.raises(ValidationError, match="内核保留 key"):
        api.evolve(HOME, task_options(metadata={key: "forged"}), security=SECURITY)


def test_legacy_write_grant_alone_cannot_reparent_another_users_memory(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    visitor = Scope(org=HOME.org, user="visitor")
    api.grant(Grant(grantor=HOME, grantee=visitor, actions=[Action.WRITE]), security=SECURITY)
    with pytest.raises(PermissionDeniedError):
        api.evolve(HOME, task_options(), security=legacy_request_context(visitor))


def test_non_hierarchy_mode_cannot_silently_ignore_hierarchy_options(api) -> None:
    options = EvolveTaskOptions(
        mode=EvolveMode.EXTRACT, hierarchy_options=task_options().hierarchy_options,
    )
    with pytest.raises(ValidationError):
        api.evolve(HOME, options, security=SECURITY)


def test_no_candidates_is_an_explicit_successful_noop(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    job_id = api.evolve(HOME, task_options(), security=SECURITY)
    status = api.job_status(job_id, security=SECURITY)
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
    job_id = api.evolve(HOME, task_options(), security=SECURITY)
    status = api.job_status(job_id, security=SECURITY)
    stored = api.get(leaf.id, leaf.scope, security=SECURITY)
    parent = api.get(stored.hierarchy.parent_id, HOME, security=SECURITY)

    assert status.status is JobStatus.FAILED
    assert status.detail["complete"] == "false"
    assert int(status.detail["repair_required_count"]) > 0
    assert "index_build_failed" in status.detail["repair_required"]
    assert parent.hierarchy.child_ids == [leaf.id], "失败不宣称回滚已写入的本体与父子边"
