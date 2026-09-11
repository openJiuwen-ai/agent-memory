# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一 SearchOptions：真实树查询、权限与单/跨空间边界透传。"""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from jiuwen_memory.api import (
    Context,
    DisclosureLevel,
    FilterClause,
    FilterOp,
    HierarchyKind,
    HierarchyRole,
    JobStatus,
    PermissionDeniedError,
    PolicyError,
    RetrievalResult,
    Scope,
    SearchOptions,
    SpaceSpec,
    ValidationError,
    assemble_runtime,
    legacy_request_context,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import iter_clauses
from jiuwen_memory.control.application.query import MemoryQueryService
from tests.unit.api.hierarchy_api_fixtures import (
    HOME,
    ROOT_SECURITY,
    SECURITY,
    START,
    runtime_config,
    task_options,
    write_snapshot,
)
from tests.unit.api.hierarchy_search_fixtures import (
    OpaqueRuntimeOption,
    hierarchy_search_options,
    snapshot_metadata,
    space_search_config,
)

pytestmark = pytest.mark.unit


@pytest.fixture(name="api", params=["in_memory", "cloud"])
def search_runtime_fixture(request):
    runtime = assemble_runtime(config=runtime_config(request.param))
    try:
        yield runtime.api
    finally:
        runtime.close()
        Factory.reset_all()


def test_real_tree_search_returns_only_requested_role_and_parent_ids(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    leaves = [write_snapshot(api, minute) for minute in (0, 1)]
    outside = write_snapshot(api, 10)
    job_id = api.evolve(HOME, task_options(), security=SECURITY)
    assert api.job_status(job_id, security=SECURITY).status is JobStatus.SUCCEEDED
    stored = api.get(leaves[0].id, leaves[0].scope, security=SECURITY)

    parents = api.search(
        "snapshot evidence", Context(HOME),
        hierarchy_search_options(hierarchy_role=HierarchyRole.TIME_SPAN), security=SECURITY,
    )
    snapshots = api.search(
        "snapshot evidence", Context(leaves[0].scope), hierarchy_search_options(),
        security=SECURITY,
    )
    all_snapshots = api.search(
        "snapshot evidence", Context(leaves[0].scope),
        hierarchy_search_options(span_start=None, span_end=None), security=SECURITY,
    )

    assert [item.unit_id for item in parents.items] == [stored.hierarchy.parent_id]
    assert parents.items[0].parent_id == ""
    assert {item.unit_id for item in snapshots.items} == {leaf.id for leaf in leaves}
    assert {item.parent_id for item in snapshots.items} == {stored.hierarchy.parent_id}
    assert {item.unit_id for item in all_snapshots.items} == {
        leaves[0].id, leaves[1].id, outside.id,
    }


def test_search_disabled_policy_only_blocks_structured_hierarchy_requests(api) -> None:
    leaf = write_snapshot(api, 0)
    ordinary = api.search("snapshot evidence", Context(leaf.scope), security=SECURITY)
    assert [item.unit_id for item in ordinary.items] == [leaf.id]
    with pytest.raises(PolicyError, match="hierarchy.enabled=false"):
        api.search("snapshot", Context(HOME), hierarchy_search_options(), security=SECURITY)


def test_empty_text_does_not_turn_hierarchy_search_into_tree_enumeration(api) -> None:
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    write_snapshot(api, 0)
    result = api.search("", Context(HOME), hierarchy_search_options(), security=SECURITY)
    assert result.items == []


@pytest.mark.parametrize("changes", [
    {"hierarchy_kind": None},
    {"hierarchy_kind": "time"},
    {"hierarchy_role": "snapshot"},
    {"span_start": None},
    {"span_end": START - timedelta(seconds=1)},
])
def test_invalid_hierarchy_options_fail_at_public_search_boundary(api, changes) -> None:
    with pytest.raises(ValidationError):
        api.search(
            "snapshot", Context(HOME), hierarchy_search_options(**changes), security=SECURITY,
        )


def test_public_search_rejects_untyped_request_and_does_not_accept_old_keywords(api) -> None:
    with pytest.raises(ValidationError, match="SearchOptions"):
        api.search("snapshot", Context(HOME), {"top_k": 1}, security=SECURITY)
    with pytest.raises(TypeError, match="top_k"):
        api.search("snapshot", Context(HOME), top_k=1, security=SECURITY)


def test_search_options_is_frozen_and_defaults_preserve_ordinary_search() -> None:
    options = SearchOptions()
    assert options.top_k == 10 and options.disclosure is DisclosureLevel.L0
    assert options.filters is None and options.hierarchy_kind is None
    with pytest.raises(FrozenInstanceError):
        options.top_k = 1


def test_single_space_request_is_copied_and_preserves_typed_and_custom_options(
    api, monkeypatch,
) -> None:
    """标准嵌套容器被快照，依赖方改写不得反向污染调用方请求。"""
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    context = Context(HOME, extensions={"max_tokens": "120", "custom": {"values": [1]}})
    options = hierarchy_search_options(
        filters=[FilterClause("source", FilterOp.EQ, "text")],
        as_of=START, disclosure=DisclosureLevel.L2, with_trajectory=True,
    )
    before_context, before_options = deepcopy(context), deepcopy(options)
    received = []

    async def capture_recall(service, scope, query):
        received.append((deepcopy(scope), deepcopy(query)))
        scope.session = "changed inside dependency"
        query.extensions["custom"]["values"].append(2)
        query.filters.value = "changed inside dependency"
        return RetrievalResult()

    monkeypatch.setattr(MemoryQueryService, "recall", capture_recall)
    api.search("snapshot", context, options, security=SECURITY)

    assert context == before_context and options == before_options
    assert len(received) == 1 and received[0][0] == HOME
    captured_request = received[0][1]
    assert captured_request.hierarchy_kind is HierarchyKind.TIME
    assert captured_request.hierarchy_role is HierarchyRole.SNAPSHOT
    assert captured_request.span_start == START
    assert captured_request.span_end == START + timedelta(minutes=1)
    assert captured_request.as_of == START and captured_request.max_tokens == 120
    assert captured_request.disclosure is DisclosureLevel.L2
    assert captured_request.with_trajectory is True
    assert captured_request.extensions == {"custom": {"values": [1]}}


def test_cross_space_hierarchy_search_preserves_permissions_and_denied_details() -> None:
    runtime = assemble_runtime(config=space_search_config())
    api = runtime.api
    owner = Scope(org=HOME.org, user=HOME.user)
    bob = Scope(org=HOME.org, user="bob")
    owner_security = legacy_request_context(owner)
    readable = Scope(org=HOME.org, space="readable")
    forbidden = Scope(org=HOME.org, space="forbidden")
    try:
        api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
        api.create_space(
            SpaceSpec(org=HOME.org, space=readable.space, owner=owner), security=ROOT_SECURITY,
        )
        api.create_space(
            SpaceSpec(org=HOME.org, space=forbidden.space, owner=bob), security=ROOT_SECURITY,
        )
        allowed = api.add(
            "snapshot allowed", readable, security=owner_security,
            system_metadata=snapshot_metadata(),
        )[0]
        api.add(
            "snapshot forbidden", forbidden, security=legacy_request_context(bob),
            system_metadata=snapshot_metadata(),
        )
        context = Context(Scope(org=HOME.org), extensions={
            "spaces": [readable.space, forbidden.space],
        })
        result = api.search(
            "snapshot", context, hierarchy_search_options(), security=owner_security,
        )
        assert [item.unit_id for item in result.items] == [allowed.id]
        assert len(result.errors) == 1 and result.errors[0].source == forbidden.space
        assert result.errors[0].error_type == "PermissionDeniedError"
        with pytest.raises(PermissionDeniedError):
            api.search(
                "snapshot", Context(forbidden), hierarchy_search_options(),
                security=owner_security,
            )
    finally:
        runtime.close()
        Factory.reset_all()


def test_hierarchy_query_does_not_bypass_permission_routing_filters(monkeypatch) -> None:
    config = runtime_config("in_memory")
    config["permission"] = {
        "default": {"target": "routing", "params": {
            "route_key": "memory_type", "fallback": "strict",
            "routes": {"profile": "strict"},
        }},
        "strict": "sqlite",
    }
    runtime = assemble_runtime(config=config)
    received = []

    async def capture_recall(service, scope, query):
        received.append(query)
        return RetrievalResult()

    monkeypatch.setattr(MemoryQueryService, "recall", capture_recall)
    try:
        runtime.api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
        runtime.api.search(
            "snapshot", Context(HOME),
            hierarchy_search_options(filters={"memory_type": "profile"}), security=SECURITY,
        )
        assert len(received) == 1
        assert received[0].hierarchy_kind is HierarchyKind.TIME
        clauses = list(iter_clauses(received[0].filters))
        assert any(
            clause.field == "system_metadata.memory_type" and clause.value == "profile"
            for clause in clauses
        )
    finally:
        runtime.close()
        Factory.reset_all()


def test_cross_space_preserves_all_typed_options_and_removes_reserved_extensions(
    monkeypatch,
) -> None:
    """跨空间分发保留结构条件及普通检索选项。"""
    runtime = assemble_runtime(config=space_search_config())
    api = runtime.api
    owner = Scope(org=HOME.org, user=HOME.user)
    requested_spaces = ["first", "second"]
    received = []

    async def capture_recall(service, scope, query):
        received.append((scope, query))
        return RetrievalResult()

    monkeypatch.setattr(MemoryQueryService, "recall", capture_recall)
    try:
        api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
        for name in requested_spaces:
            api.create_space(
                SpaceSpec(org=HOME.org, space=name, owner=owner), security=ROOT_SECURITY,
            )
        context = Context(Scope(org=HOME.org), extensions={
            "spaces": requested_spaces, "max_tokens": "321", "custom": "kept",
        })
        options = hierarchy_search_options(
            filters=[FilterClause("source", FilterOp.EQ, "text")],
            as_of=START, disclosure=DisclosureLevel.L1, with_trajectory=True, top_k=4,
        )
        original_context = deepcopy(context)
        api.search("snapshot", context, options, security=legacy_request_context(owner))

        assert context == original_context
        assert {scope.space for scope, _ in received} == set(requested_spaces)
        for target_scope, request in received:
            assert target_scope.org == HOME.org
            assert request.hierarchy_kind is HierarchyKind.TIME
            assert request.hierarchy_role is HierarchyRole.SNAPSHOT
            assert request.span_start == START
            assert request.span_end == START + timedelta(minutes=1)
            assert request.as_of == START and request.max_tokens == 321
            assert request.disclosure is DisclosureLevel.L1 and request.with_trajectory
            assert request.top_k > 0 and request.top_k <= options.top_k
            assert request.extensions == {"custom": "kept"}
            assert any(clause.field == "source" for clause in iter_clauses(request.filters))
    finally:
        runtime.close()
        Factory.reset_all()


@pytest.mark.parametrize("across", [False, True])
def test_runtime_extensions_keep_opaque_plugin_identity(across, monkeypatch) -> None:
    runtime = assemble_runtime(config=space_search_config())
    owner = Scope(org=HOME.org, user=HOME.user)
    opaque = OpaqueRuntimeOption()
    received = []

    async def capture_recall(service, scope, query):
        received.append(query.extensions["plugin"]["instances"][0])
        return RetrievalResult()

    monkeypatch.setattr(MemoryQueryService, "recall", capture_recall)
    try:
        for name in ("first", "second"):
            runtime.api.create_space(
                SpaceSpec(org=HOME.org, space=name, owner=owner), security=ROOT_SECURITY,
            )
        context = Context(Scope(org=HOME.org, space="first"), extensions={
            "plugin": {"instances": [opaque]}, "max_tokens": "123",
        })
        if across:
            context.extensions["spaces"] = ["first", "second"]
        runtime.api.search("snapshot", context, security=legacy_request_context(owner))

        assert len(received) == (2 if across else 1)
        assert all(instance is opaque for instance in received)
        assert context.extensions["plugin"]["instances"][0] is opaque
        assert context.extensions["max_tokens"] == "123"
        if across:
            assert context.extensions["spaces"] == ["first", "second"]
    finally:
        runtime.close()
        Factory.reset_all()
