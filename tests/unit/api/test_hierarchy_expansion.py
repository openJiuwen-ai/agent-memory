"""公开写入→显式建树→展开证据，以及公开请求/序列化边界。"""


from dataclasses import replace

import pytest

from jiuwen_memory.api import (
    Context,
    HierarchyRole,
    JobStatus,
    Scope,
    SpaceSpec,
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
    job_id = api.evolve(HOME, **vars(task_options()), security=security_for(api, HOME))
    assert api.job_status(job_id, security=security_for(api, HOME)).status is JobStatus.SUCCEEDED
    options = hierarchy_search_options(hierarchy_role=HierarchyRole.TIME_SPAN, expand_depth=1)
    result = api.search(
        "snapshot evidence", Context(HOME), **vars(options), security=security_for(api, HOME),
    )
    assert len(result.items) == 3
    assert {item.unit_id for item in result.items[1:]} == {leaf.id for leaf in leaves}
    assert all(item.parent_id == result.items[0].unit_id for item in result.items[1:])
    assert result.errors == []


def test_disabled_search_ignores_expansion(api) -> None:
    api.admin_set("hierarchy.enabled", "false", security=ROOT_SECURITY)
    api.search(
        "snapshot", Context(HOME), **vars(hierarchy_search_options(expand_depth=1)),
        security=security_for(api, HOME),
    )


@pytest.mark.parametrize("depth", [-1, True, "1", None])
def test_invalid_depth_fails_at_public_boundary(api, depth) -> None:
    with pytest.raises(ValidationError, match="expand_depth"):
        api.search(
            "snapshot", Context(HOME), **vars(hierarchy_search_options(expand_depth=depth)),
            security=security_for(api, HOME),
        )


def test_http_cli_shared_contract_serializes_only_flat_public_results(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    leaf = write_snapshot(api, 0, session="")
    job_id = api.evolve(HOME, **vars(task_options()), security=security_for(api, HOME))
    assert api.job_status(job_id, security=security_for(api, HOME)).status is JobStatus.SUCCEEDED
    result = invoke_api(api, "search", {
        "query": "snapshot evidence",
        "context": {"scope": {"org": HOME.org, "user": HOME.user, "agent": HOME.agent}},
        **{
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
        leaves = [write_snapshot(api, minute, session="") for minute in (0, 1)]
        security = security_for(api, HOME)
        job_id = api.evolve(HOME, **vars(task_options(depth)), security=security)
        info = api.job_status(job_id, security=security)
        assert info.status is JobStatus.SUCCEEDED, info.detail
        options = hierarchy_search_options(
            hierarchy_role=role, expand_depth=depth, rollup=True,
        )
        result = api.search("snapshot evidence", Context(HOME), **vars(options), security=security)
        assert len(result.items) == depth + len(leaves)
        assert {leaf.id for leaf in leaves} <= {item.unit_id for item in result.items[1:]}
        assert not result.errors
    finally:
        runtime.close()
        Factory.reset_all()


def test_real_cross_space_search_applies_one_total_limit_after_expansion() -> None:
    runtime = assemble_runtime(config=space_search_config())
    api = runtime.api
    owner = Scope(org=HOME.org, user=HOME.user)
    security = security_for(api, owner)
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
            job_id = api.evolve(home, **vars(task_options(tree_home_scope=home)), security=security)
            assert api.job_status(job_id, security=security).status is JobStatus.SUCCEEDED
        result = api.search("snapshot evidence", Context(Scope(org=HOME.org), extensions={
            "spaces": ["one", "two"],
        }), **vars(hierarchy_search_options(
            hierarchy_role=HierarchyRole.TIME_SPAN, expand_depth=1, top_k=2,
        )), security=security)
        assert len(result.items) == 2
        assert not {item.unit_id for item in result.items} & {leaf.id for leaf in leaves}
        assert result.errors == []
    finally:
        runtime.close()
        Factory.reset_all()


@pytest.mark.parametrize("use_defaults", [True, False])
@pytest.mark.parametrize("top_k", [1, 2])
def test_public_tree_search_caps_total_without_changing_qa_prefix(api, use_defaults, top_k):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    for minute in (0, 1):
        write_snapshot(api, minute, session="")
    job_id = api.evolve(HOME, **vars(task_options()), security=security_for(api, HOME))
    assert api.job_status(job_id, security=security_for(api, HOME)).status is JobStatus.SUCCEEDED

    def search(limit):
        if use_defaults:
            return api.search(
                "snapshot evidence", Context(HOME), top_k=limit, security=security_for(api, HOME),
            )
        options = hierarchy_search_options(
            hierarchy_role=HierarchyRole.TIME_SPAN, expand_depth=1, rollup=True, top_k=limit,
        )
        return api.search(
            "snapshot evidence", Context(HOME), **vars(options), security=security_for(api, HOME),
        )

    full = search(10)
    limited = search(top_k)
    assert len(full.items) == 3
    assert len(limited.items) == top_k
    assert limited.items == full.items[:top_k], "默认和显式参数都必须保留 QA 使用的前缀"
