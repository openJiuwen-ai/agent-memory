"""公开 SearchOptions、真实运行时与跨空间上卷/展开组合。"""

import pytest

from jiuwen_memory.api import (
    Context,
    HierarchyRole,
    JobStatus,
    PolicyError,
    Scope,
    SearchOptions,
    SpaceSpec,
    ValidationError,
    assemble_runtime,
    legacy_request_context,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory_entry.core.api_contract import invoke_api
from tests.unit.api.hierarchy_api_fixtures import (
    HOME,
    ROOT_SECURITY,
    SECURITY,
    runtime_config,
    task_options,
    write_snapshot,
)
from tests.unit.api.hierarchy_search_fixtures import (
    hierarchy_search_options,
    snapshot_metadata,
    space_search_config,
)

pytestmark = pytest.mark.unit


@pytest.fixture(name="api", params=["in_memory", "cloud"])
def rollup_runtime_fixture(request):
    runtime = assemble_runtime(config=runtime_config(request.param))
    try:
        yield runtime.api
    finally:
        runtime.close()
        Factory.reset_all()


def test_public_rollup_returns_parent_and_optional_original_evidence(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    leaves = [write_snapshot(api, minute, session="") for minute in (0, 1)]
    job_id = api.evolve(HOME, task_options(), security=SECURITY)
    assert api.job_status(job_id, security=SECURITY).status is JobStatus.SUCCEEDED
    options = hierarchy_search_options(
        hierarchy_role=HierarchyRole.TIME_SPAN, rollup=True, with_trajectory=True,
    )
    result = api.search("snapshot evidence", Context(HOME), options, security=SECURITY)
    assert len(result.items) == 1
    assert result.items[0].unit_id not in {leaf.id for leaf in leaves}
    assert any(step.stage == "rollup" for step in result.trajectory)
    result = invoke_api(api, "search", {
        "query": "snapshot evidence",
        "context": {"scope": {"org": HOME.org, "user": HOME.user, "agent": HOME.agent}},
        "options": {
            "hierarchy_kind": "time", "hierarchy_role": "time_span",
            "rollup": True, "expand_depth": 1,
        },
    }, SECURITY)
    assert set(result) == {"items", "trajectory", "errors"}
    assert {item["unit_id"] for item in result["items"][1:]} == {leaf.id for leaf in leaves}
    assert result["errors"] == []


@pytest.mark.parametrize("value", [1, "true", None])
def test_invalid_public_rollup_is_rejected(api, value) -> None:
    with pytest.raises(ValidationError, match="rollup"):
        api.search("snapshot", Context(HOME), hierarchy_search_options(rollup=value),
                   security=SECURITY)


def test_rollup_requires_kind_and_enabled_policy(api) -> None:
    with pytest.raises(ValidationError, match="hierarchy_kind"):
        api.search("snapshot", Context(HOME), SearchOptions(rollup=True), security=SECURITY)
    with pytest.raises(PolicyError, match="hierarchy.enabled"):
        api.search("snapshot", Context(HOME), hierarchy_search_options(rollup=True),
                   security=SECURITY)


def test_session_restricted_search_does_not_roll_up_into_broader_home(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    leaf = write_snapshot(api, 0)
    job_id = api.evolve(HOME, task_options(), security=SECURITY)
    assert api.job_status(job_id, security=SECURITY).status is JobStatus.SUCCEEDED
    result = api.search("snapshot evidence", Context(leaf.scope), hierarchy_search_options(
        hierarchy_role=HierarchyRole.TIME_SPAN, rollup=True,
    ), security=SECURITY)
    assert result.items == []
    assert any(error.error_type == "scope_excluded" for error in result.errors)


def test_real_cross_space_rollup_preserves_two_evidence_groups() -> None:
    runtime = assemble_runtime(config=space_search_config())
    api = runtime.api
    owner = Scope(org=HOME.org, user=HOME.user)
    security = legacy_request_context(owner)
    leaves = []
    try:
        api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
        for name in ("one", "two"):
            api.create_space(SpaceSpec(org=HOME.org, space=name, owner=owner),
                             security=ROOT_SECURITY)
            home = Scope(org=HOME.org, space=name)
            leaf = api.add(f"snapshot evidence {name}", home,
                           system_metadata=snapshot_metadata(), security=security)[0]
            leaves.append(leaf)
            job_id = api.evolve(home, task_options(tree_home_scope=home), security=security)
            assert api.job_status(job_id, security=security).status is JobStatus.SUCCEEDED
        result = api.search("snapshot evidence", Context(Scope(org=HOME.org), extensions={
            "spaces": ["one", "two"],
        }), hierarchy_search_options(
            hierarchy_role=HierarchyRole.TIME_SPAN, rollup=True, expand_depth=1,
            top_k=2, with_trajectory=True,
        ), security=security)
        assert len(result.items) == 4
        expected_ids = {expected_leaf.id for expected_leaf in leaves}
        assert {item.unit_id for item in result.items[2:]} == expected_ids
        assert len([step for step in result.trajectory if step.stage == "rollup"]) == 2
        assert result.errors == []
    finally:
        runtime.close()
        Factory.reset_all()
