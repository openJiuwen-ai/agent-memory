"""公开写入→显式建树→展开证据，以及公开请求/序列化边界。"""

from dataclasses import replace

import pytest

from jiuwen_memory.api import (
    Context,
    HierarchyRole,
    JobStatus,
    PolicyError,
    Scope,
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
    job_id = api.evolve(HOME, task_options(), security=SECURITY)
    assert api.job_status(job_id, security=SECURITY).status is JobStatus.SUCCEEDED
    options = hierarchy_search_options(hierarchy_role=HierarchyRole.TIME_SPAN, expand_depth=1)
    result = api.search("snapshot evidence", Context(HOME), options, security=SECURITY)
    assert len(result.items) == 3
    assert {item.unit_id for item in result.items[1:]} == {leaf.id for leaf in leaves}
    assert all(item.parent_id == result.items[0].unit_id for item in result.items[1:])
    assert result.errors == []


def test_expansion_still_requires_enabled_policy(api) -> None:
    with pytest.raises(PolicyError, match="hierarchy.enabled"):
        api.search("snapshot", Context(HOME), hierarchy_search_options(expand_depth=1),
                   security=SECURITY)


@pytest.mark.parametrize("depth", [-1, True, "1", None])
def test_invalid_depth_fails_at_public_boundary(api, depth) -> None:
    with pytest.raises(ValidationError, match="expand_depth"):
        api.search("snapshot", Context(HOME), hierarchy_search_options(expand_depth=depth),
                   security=SECURITY)


def test_http_cli_shared_contract_serializes_only_flat_public_results(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    leaf = write_snapshot(api, 0)
    job_id = api.evolve(HOME, task_options(), security=SECURITY)
    assert api.job_status(job_id, security=SECURITY).status is JobStatus.SUCCEEDED
    result = invoke_api(api, "search", {
        "query": "snapshot evidence",
        "context": {"scope": {"org": HOME.org, "user": HOME.user, "agent": HOME.agent}},
        "options": {
            "hierarchy_kind": "time", "hierarchy_role": "time_span", "expand_depth": 1,
        },
    }, SECURITY)
    assert set(result) == {"items", "trajectory", "errors"}
    assert result["items"][1]["unit_id"] == leaf.id
    assert "expansion_roots" not in result


def test_real_cross_space_search_keeps_both_parent_evidence_groups() -> None:
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
            leaf_scope = replace(home, session="conversation")
            leaf = api.add(f"snapshot evidence {name}", leaf_scope,
                           system_metadata=snapshot_metadata(), security=security)[0]
            leaves.append(leaf)
            job_id = api.evolve(home, task_options(tree_home_scope=home), security=security)
            assert api.job_status(job_id, security=security).status is JobStatus.SUCCEEDED
        result = api.search("snapshot evidence", Context(Scope(org=HOME.org), extensions={
            "spaces": ["one", "two"],
        }), hierarchy_search_options(
            hierarchy_role=HierarchyRole.TIME_SPAN, expand_depth=1, top_k=2,
        ), security=security)
        assert len(result.items) == 4
        assert {item.unit_id for item in result.items[2:]} == {leaf.id for leaf in leaves}
        assert result.errors == []
    finally:
        runtime.close()
        Factory.reset_all()
