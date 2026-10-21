"""普通 API 查询继承树预设，显式层级组、权限及关闭语义保持严格。"""

from copy import deepcopy
from dataclasses import replace

import pytest

from jiuwen_memory.api import (
    Context,
    DisclosureLevel,
    FilterClause,
    FilterOp,
    HierarchyKind,
    HierarchyRole,
    RetrievalResult,
    SearchOptions,
    ValidationError,
    assemble_runtime,
)
from jiuwen_memory.api.memory_api_impl.search_defaults import with_hierarchy_defaults
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import iter_clauses
from jiuwen_memory.control.application.query import MemoryQueryService
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


@pytest.mark.parametrize("legacy", [False, True])
def test_enabled_plain_search_applies_timespan_defaults_and_preserves_options(
    api, monkeypatch, legacy,
):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    filters = [FilterClause("user_metadata.source", FilterOp.EQ, "session-0")]
    options = SearchOptions(
        top_k=7, filters=filters, as_of=START, disclosure=DisclosureLevel.L2,
        with_trajectory=True,
    )
    before = deepcopy(options)
    captured = []

    async def recall(service, scope, query):
        captured.append((scope, query))
        return RetrievalResult()

    monkeypatch.setattr(MemoryQueryService, "recall", recall)
    if legacy:
        api.search(
            "question", Context(HOME), security=security_for(api, HOME), top_k=options.top_k,
            filters=filters, as_of=START, disclosure=options.disclosure, with_trajectory=True,
        )
    else:
        api.search_v2("question", Context(HOME), options, security=security_for(api, HOME))
    assert options == before
    scope, query = captured[0]
    assert scope == HOME
    assert (query.hierarchy_kind, query.hierarchy_role) == (
        HierarchyKind.TIME, HierarchyRole.TIME_SPAN,
    )
    assert query.expand_depth == 1 and query.rollup is True
    assert query.top_k == 7 and query.disclosure is DisclosureLevel.L2
    assert query.as_of == START and query.with_trajectory is True
    assert any(clause.field == "user_metadata.source" and clause.value == "session-0"
               for clause in iter_clauses(query.filters))


@pytest.mark.parametrize("role", [
    HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.SNAPSHOT,
])
def test_explicit_hierarchy_group_keeps_false_and_zero(api, monkeypatch, role):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    options = SearchOptions(
        hierarchy_kind=HierarchyKind.TIME, hierarchy_role=role, rollup=False, expand_depth=0,
    )
    captured = []

    async def recall(service, scope, query):
        captured.append(query)
        return RetrievalResult()

    monkeypatch.setattr(MemoryQueryService, "recall", recall)
    api.search_v2("question", Context(HOME), options, security=security_for(api, HOME))
    assert captured[0].hierarchy_role is role
    assert captured[0].rollup is False and captured[0].expand_depth == 0


def test_disabled_plain_search_still_recalls_snapshot(api):
    api.admin_set("hierarchy.enabled", "false", security=ROOT_SECURITY)
    leaf = write_snapshot(api, 0)
    result = api.search("snapshot evidence", Context(leaf.scope), security=security_for(api, HOME))
    assert [item.unit_id for item in result.items] == [leaf.id]


def test_missing_timespan_does_not_fall_back_to_unattached_snapshot(api):
    leaf = write_snapshot(api, 0)
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    result = api.search("snapshot evidence", Context(leaf.scope), security=security_for(api, HOME))
    assert result.items == [], "默认 TimeSpan 查询不回退到未挂树 Snapshot"


def test_default_search_returns_parent_and_its_children_after_build(api):
    leaf = write_snapshot(api, 0, session="")
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    api.evolve_v2(HOME, task_options(), security=security_for(api, HOME))
    stored = api.get(leaf.id, leaf.scope, security=security_for(api, HOME))
    result = api.search("snapshot evidence", Context(HOME), security=security_for(api, HOME))
    assert [item.unit_id for item in result.items] == [stored.hierarchy.parent_id, leaf.id]


@pytest.mark.parametrize("options", [
    SearchOptions(rollup=True), SearchOptions(expand_depth=1),
    SearchOptions(expand_depth=False), SearchOptions(expand_depth=None),
    SearchOptions(rollup=None), SearchOptions(hierarchy_role=HierarchyRole.SCENE),
])
def test_default_resolution_does_not_hide_invalid_explicit_options(api, options):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    with pytest.raises(ValidationError):
        api.search_v2("question", Context(HOME), options, security=security_for(api, HOME))


def test_resolver_is_pure_and_supports_copied_options():
    original = SearchOptions(top_k=23)
    for options in (original, deepcopy(original), replace(original)):
        resolved = with_hierarchy_defaults(options, enabled=True)
        assert resolved.top_k == 23 and resolved.hierarchy_role is HierarchyRole.TIME_SPAN
        assert options.hierarchy_kind is None
        assert with_hierarchy_defaults(options, enabled=False) == original


@pytest.mark.parametrize("explicit", [False, True])
def test_transport_decoding_preserves_default_vs_explicit_hierarchy_group(
    api, monkeypatch, explicit,
):
    from jiuwen_memory_entry.core.api_contract import invoke_api

    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    captured = []

    async def recall(service, scope, query):
        captured.append(query)
        return RetrievalResult()

    monkeypatch.setattr(MemoryQueryService, "recall", recall)
    options = {"top_k": 9}
    if explicit:
        options.update(hierarchy_kind="time", hierarchy_role="scene", expand_depth=0, rollup=False)
    invoke_api(api, "search_v2", {
        "query": "question",
        "context": {"scope": {"org": HOME.org, "user": HOME.user, "agent": HOME.agent}},
        "options": options,
    }, security_for(api, HOME))
    assert captured[0].top_k == 9
    assert captured[0].hierarchy_role is (
        HierarchyRole.SCENE if explicit else HierarchyRole.TIME_SPAN
    )
    assert captured[0].expand_depth == (0 if explicit else 1)
    assert captured[0].rollup is (not explicit)


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("across", [True, False])
def test_untrusted_search_never_reads_default_policy(api, monkeypatch, legacy, across):
    from unittest.mock import Mock

    from jiuwen_memory.common.errors import PermissionDeniedError
    from jiuwen_memory.common.security.types import AuthContext, RequestSecurityContext
    from jiuwen_memory.control.policy_impl.dict_policy_manager import DictPolicyManager

    read = Mock(side_effect=AssertionError("untrusted request read policy"))
    monkeypatch.setattr(DictPolicyManager, "all", read)
    untrusted = RequestSecurityContext(auth=AuthContext(actor=HOME))
    context = Context(HOME, extensions={"spaces": ["unreadable"]} if across else {})
    method = api.search if legacy else api.search_v2
    with pytest.raises(PermissionDeniedError):
        method("question", context, security=untrusted)
    read.assert_not_called()
