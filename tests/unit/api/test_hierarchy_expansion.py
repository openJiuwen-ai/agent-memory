"""公开写入→显式建树→展开证据，以及公开请求/序列化边界。"""


import pytest

from jiuwen_memory.api import (
    Context,
    HierarchyRole,
    JobStatus,
    PolicyError,
    ValidationError,
    assemble_runtime,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory_entry.core.api_contract import invoke_api
from tests.unit.api.fixtures import ROOT_SECURITY, security_for
from tests.unit.api.hierarchy_api_fixtures import (
    HOME,
    runtime_config,
    task_options,
    write_snapshot,
)
from tests.unit.api.hierarchy_search_fixtures import (
    hierarchy_search_options,
)

pytestmark = pytest.mark.unit


@pytest.fixture(name="api", params=["in_memory", "cloud"])
def expansion_runtime_fixture(request):
    runtime = assemble_runtime(config=runtime_config(request.param))
    try:
        yield runtime.api
    finally:
        runtime.close()
        Factory.reset_all()


def test_public_search_returns_built_parents_and_original_leaf_evidence(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    leaves = [write_snapshot(api, minute) for minute in (0, 1)]
    job_id = api.evolve_v2(HOME, task_options(), security=security_for(api, HOME))
    assert api.job_status(job_id, security=security_for(api, HOME)).status is JobStatus.SUCCEEDED
    options = hierarchy_search_options(hierarchy_role=HierarchyRole.TIME_SPAN, expand_depth=1)
    result = api.search_v2(
        "snapshot evidence", Context(HOME), options, security=security_for(api, HOME),
    )
    assert len(result.items) == 3
    assert {item.unit_id for item in result.items[1:]} == {leaf.id for leaf in leaves}
    assert all(item.parent_id == result.items[0].unit_id for item in result.items[1:])
    assert result.errors == []


def test_expansion_still_requires_enabled_policy(api) -> None:
    api.admin_set("hierarchy.enabled", "false", security=ROOT_SECURITY)
    with pytest.raises(PolicyError, match="hierarchy.enabled"):
        api.search_v2(
            "snapshot", Context(HOME), hierarchy_search_options(expand_depth=1),
            security=security_for(api, HOME),
        )


@pytest.mark.parametrize("depth", [-1, True, "1", None])
def test_invalid_depth_fails_at_public_boundary(api, depth) -> None:
    with pytest.raises(ValidationError, match="expand_depth"):
        api.search_v2(
            "snapshot", Context(HOME), hierarchy_search_options(expand_depth=depth),
            security=security_for(api, HOME),
        )


def test_http_cli_shared_contract_serializes_only_flat_public_results(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    leaf = write_snapshot(api, 0)
    job_id = api.evolve_v2(HOME, task_options(), security=security_for(api, HOME))
    assert api.job_status(job_id, security=security_for(api, HOME)).status is JobStatus.SUCCEEDED
    result = invoke_api(api, "search_v2", {
        "query": "snapshot evidence",
        "context": {"scope": {"org": HOME.org, "user": HOME.user, "agent": HOME.agent}},
        "options": {
            "hierarchy_kind": "time", "hierarchy_role": "time_span", "expand_depth": 1,
        },
    }, security_for(api, HOME))
    assert set(result) == {"items", "trajectory", "errors"}
    assert result["items"][1]["unit_id"] == leaf.id
    assert "expansion_roots" not in result


@pytest.mark.parametrize("engine", ["in_memory", "cloud"])
@pytest.mark.parametrize("depth,role", [
    (1, HierarchyRole.TIME_SPAN), (2, HierarchyRole.SCENE), (3, HierarchyRole.EVENT),
])
def test_each_built_level_expands_to_original_evidence(engine, depth, role):
    runtime = assemble_runtime(config=runtime_config(engine, depth))
    api = runtime.api
    try:
        leaves = [write_snapshot(api, minute) for minute in (0, 1)]
        security = security_for(api, HOME)
        job_id = api.evolve_v2(HOME, task_options(depth), security=security)
        info = api.job_status(job_id, security=security)
        assert info.status is JobStatus.SUCCEEDED, info.detail
        options = hierarchy_search_options(
            hierarchy_role=role, expand_depth=depth, rollup=True,
        )
        result = api.search_v2("snapshot evidence", Context(HOME), options, security=security)
        assert len(result.items) == depth + len(leaves)
        assert {item.unit_id for item in result.items[-2:]} == {leaf.id for leaf in leaves}
        assert not result.errors
    finally:
        runtime.close()
        Factory.reset_all()
