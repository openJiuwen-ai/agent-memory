# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""公开默认值、树总开关和原接口兼容性，覆盖本地与云端。"""

import inspect
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import (
    Context,
    DisclosureLevel,
    FilterClause,
    FilterOp,
    HierarchyKind,
    HierarchyRole,
    MemoryAPI,
    PermissionDeniedError,
    RetrievalResult,
    assemble_runtime,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.types import AuthContext, RequestSecurityContext
from jiuwen_memory.common.type_def import iter_clauses
from jiuwen_memory.control.application.query import MemoryQueryService
from jiuwen_memory.control.policy_impl.dict_policy_manager import DictPolicyManager
from jiuwen_memory_entry.core.api_contract import invoke_api
from tests.unit.api.fixtures import ROOT_SECURITY, security_for
from tests.unit.api.hierarchy_api_fixtures import (
    HOME,
    START,
    runtime_config,
    task_options,
    write_snapshot,
)

pytestmark = pytest.mark.unit


@pytest.fixture(name="api", params=["in_memory", "cloud"])
def runtime_fixture(request):
    runtime = assemble_runtime(config=runtime_config(request.param))
    try:
        yield runtime.api
    finally:
        runtime.close()
        Factory.reset_all()


@pytest.mark.parametrize("overrides,depth,rollup", [
    ({}, 1, True), ({"expand_depth": 0}, 0, True), ({"rollup": False}, 1, False),
    ({"expand_depth": 0, "rollup": False}, 0, False),
])
def test_enabled_search_uses_signature_defaults_and_explicit_values(
    api, monkeypatch, overrides, depth, rollup,
):
    filters = [FilterClause("user_metadata.source", FilterOp.EQ, "session-0")]
    before = deepcopy(filters)
    captured = []

    async def recall(service, scope, query):
        captured.append(query)
        return RetrievalResult()

    monkeypatch.setattr(MemoryQueryService, "recall", recall)
    api.search("question", Context(HOME), security=security_for(api, HOME), filters=filters,
               as_of=START, top_k=7, disclosure=DisclosureLevel.L2,
               with_trajectory=True, **overrides)
    query = captured[0]
    assert (query.hierarchy_kind, query.hierarchy_role) == (
        HierarchyKind.TIME, HierarchyRole.TIME_SPAN,
    )
    assert query.expand_depth == depth and query.rollup is rollup
    assert query.top_k == 7 and query.disclosure is DisclosureLevel.L2
    assert query.as_of == START and query.with_trajectory is True
    assert any(clause.field == "user_metadata.source" for clause in iter_clauses(query.filters))
    assert filters == before


@pytest.mark.parametrize("tree", [
    {},
    {"hierarchy_kind": HierarchyKind.TIME, "hierarchy_role": HierarchyRole.SCENE,
     "span_start": START, "span_end": START, "expand_depth": 3, "rollup": True},
    {"hierarchy_kind": "invalid", "hierarchy_role": "invalid", "span_start": "bad-time",
     "span_end": None, "expand_depth": -1, "rollup": None},
])
def test_disabled_search_clears_all_tree_parameters_before_semantic_validation(
    api, monkeypatch, tree,
):
    api.admin_set("hierarchy.enabled", "false", security=ROOT_SECURITY)
    captured = []
    before = deepcopy(tree)

    async def recall(service, scope, query):
        captured.append(query)
        return RetrievalResult()

    monkeypatch.setattr(MemoryQueryService, "recall", recall)
    api.search("question", Context(HOME), security=security_for(api, HOME), top_k=7,
               as_of=START, filters={"tags": {"contains": "note"}}, **tree)
    query = captured[0]
    assert query.hierarchy_kind is None and query.hierarchy_role is None
    assert query.span_start is None and query.span_end is None
    assert query.expand_depth == 0 and query.rollup is False
    assert query.top_k == 7 and query.as_of == START
    assert any(clause.field == "tags" for clause in iter_clauses(query.filters))
    assert tree == before


def test_missing_timespan_does_not_fall_back_to_snapshot(api):
    leaf = write_snapshot(api, 0)
    result = api.search("snapshot evidence", Context(leaf.scope), security=security_for(api, HOME))
    assert result.items == []


def test_default_search_returns_parent_and_children_after_build(api):
    leaf = write_snapshot(api, 0, session="")
    api.evolve(HOME, **vars(task_options()), security=security_for(api, HOME))
    stored = api.get(leaf.id, leaf.scope, security=security_for(api, HOME))
    result = api.search("snapshot evidence", Context(HOME), security=security_for(api, HOME))
    assert [item.unit_id for item in result.items] == [stored.hierarchy.parent_id, leaf.id]


@pytest.mark.parametrize("explicit", [False, True])
def test_transport_uses_same_defaults_and_preserves_zero_false(api, monkeypatch, explicit):
    captured = []

    async def recall(service, scope, query):
        captured.append(query)
        return RetrievalResult()

    monkeypatch.setattr(MemoryQueryService, "recall", recall)
    request = {"query": "question", "context": {"scope": vars(HOME)}, "top_k": 9}
    if explicit:
        request.update(hierarchy_role="scene", expand_depth=0, rollup=False)
    invoke_api(api, "search", request, security_for(api, HOME))
    query = captured[0]
    assert query.hierarchy_role is (HierarchyRole.SCENE if explicit else HierarchyRole.TIME_SPAN)
    assert query.top_k == 9 and query.expand_depth == (0 if explicit else 1)
    assert query.rollup is (not explicit)


@pytest.mark.parametrize("across", [True, False])
def test_untrusted_search_never_reads_policy(api, monkeypatch, across):
    read = Mock(side_effect=AssertionError("untrusted request read policy"))
    monkeypatch.setattr(DictPolicyManager, "all", read)
    untrusted = RequestSecurityContext(auth=AuthContext(actor=HOME))
    context = Context(HOME, extensions={"spaces": ["unreadable"]} if across else {})
    with pytest.raises(PermissionDeniedError):
        api.search("question", context, security=untrusted)
    read.assert_not_called()


def test_public_signatures_keep_original_arguments_and_declare_tree_defaults(api):
    original_search = ("self", "query", "context", "security", "filters", "as_of", "top_k",
                       "disclosure", "with_trajectory")
    original_evolve = ("self", "scope", "mode", "channel", "candidate", "dreaming", "interval",
                       "security")
    for method, original in (("search", original_search), ("evolve", original_evolve)):
        public = inspect.signature(getattr(MemoryAPI, method))
        implementation = inspect.signature(getattr(type(api), method))
        assert public == implementation
        assert tuple(public.parameters)[:len(original)] == original
    defaults = inspect.signature(MemoryAPI.search).parameters
    assert defaults["hierarchy_kind"].default is HierarchyKind.TIME
    assert defaults["hierarchy_role"].default is HierarchyRole.TIME_SPAN
    assert defaults["expand_depth"].default == 1 and defaults["rollup"].default is True
