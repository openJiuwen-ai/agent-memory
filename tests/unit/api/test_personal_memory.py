"""个人查询绑定不会跳过 MemoryAPI 授权。"""

from unittest.mock import Mock

import pytest

from jiuwen_memory.api import (
    Context,
    MemoryRef,
    MemoryUnit,
    Scope,
    ViewBudget,
    read_personal_record,
)
from jiuwen_memory.api.personal_memory import PersonalScopeBinding, search_personal_records
from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    PermissionDeniedError,
    UnsupportedCapabilityError,
    ValidationError,
)
from jiuwen_memory.control.application.query import MemoryQueryService
from jiuwen_memory.control.types import DeleteMode, DeleteSelector, MemoryListResult
from jiuwen_memory.retrieval.types import ChannelError, RetrievalResult

pytestmark = pytest.mark.unit


def test_personal_api_reuses_list_and_search_and_limits_record_checks(monkeypatch):
    from jiuwen_memory.api import assemble_runtime, build_dev_authenticator, internal_context

    runtime = assemble_runtime()
    try:
        api = runtime.api
        security = internal_context(build_dev_authenticator())
        scopes = [
            Scope(org="local", user="developer", agent="design", session=session)
            for session in ("one", "two")
        ]
        first = api.add("release checklist", scopes[0], security=security)[0]
        api.add("deployment notes", scopes[1], tags=["release"], security=security)
        api.add("other note", scopes[0], security=security)
        api.delete(
            DeleteSelector(scope=scopes[0], unit_ids=[first.id], mode=DeleteMode.FORGET),
            security=security,
        )
        assert isinstance(api.list(scopes[0], security=security), MemoryListResult)
        with pytest.raises(ValidationError, match="scope"):
            api.list(security=security)

        queried = []
        original = MemoryQueryService.list_with_permission_contexts

        async def read(service, scope, **kwargs):
            queried.append(scope)
            return await original(service, scope, **kwargs)

        monkeypatch.setattr(MemoryQueryService, "list_with_permission_contexts", read)
        personal = api.list(
            security=security,
            limit=1,
            extensions={
                "personal": {"query": " RELEASE ", "include_lifecycle_counts": True},
            },
        )
        assert queried == scopes
        assert personal.count == 1
        assert personal.items[0].scope.session == "two"
        assert personal.lifecycle_counts == {
            "active": 1, "superseded": 0, "forgotten": 1, "archived": 0,
        }
        personal_context = Context(extensions={"personal": {}})
        hits = api.search("deployment", personal_context, security=security)
        assert hits.items[0].scope.session == "two"
        for view_options in (
            {"task_id": "task"},
            {"anchor": MemoryRef.of(first)},
            {"duty": "general"},
            {"view_budget": ViewBudget()},
        ):
            with pytest.raises(ValidationError, match="personal search only accepts"):
                api.search("deployment", personal_context, security=security, **view_options)

        with pytest.raises(ValidationError, match="personal list options"):
            api.list(scopes[0], extensions={"personal": {}}, security=security)
        monkeypatch.setattr(MemoryQueryService, "candidate_scopes", Mock(
            side_effect=AssertionError("single record operation must not enumerate scopes"),
        ))
        unit = personal.items[0]
        detail = read_personal_record(api, unit.scope, unit.id, security=security)
        assert detail.scope.agent == "design"
    finally:
        runtime.close()


def test_partial_retrieval_failure_is_not_treated_as_complete():
    api = Mock()

    api.list.return_value = MemoryListResult([MemoryUnit(id="one", scope=Scope(org="a"))], 1)
    api.search.return_value = RetrievalResult(
        errors=[ChannelError(channel="test", source="test", error_type="error", message="down")]
    )
    with pytest.raises(BackendError):
        search_personal_records(
            api, [PersonalScopeBinding(Scope(org="a"))], "query", security=object(),
            read_scope=api.list, skip_candidate=lambda _unit: False, top_k=1, source=None,
        )


def test_agent_personal_view_checks_author_in_a_scope_without_user():
    from jiuwen_memory.api import assemble_runtime, internal_context
    from jiuwen_memory.common.security.types import Role
    from tests.support.scoped_authenticator import ScopedAuthenticator

    scope = Scope(org="local", agent="assistant")
    agent = internal_context(ScopedAuthenticator(scope, role=Role.ROOT))
    user = internal_context(
        ScopedAuthenticator(Scope(org="local", user="owner"), role=Role.ROOT)
    )
    runtime = assemble_runtime()
    try:
        other = runtime.api.add("user note", scope, security=user)[0]
        own = runtime.api.add("agent note", scope, security=agent)[0]
        page = runtime.api.list(security=agent, extensions={"personal": {}})
        assert [unit.id for unit in page.items] == [own.id]
        with pytest.raises(PermissionDeniedError):
            read_personal_record(runtime.api, scope, other.id, security=agent)
    finally:
        runtime.close()


@pytest.mark.parametrize("count,items,error", [
    (10001, [], UnsupportedCapabilityError),
    (2, [], ConflictError),
    (1, None, ValidationError),
])
def test_search_rejects_incomplete_or_foreign_candidate_scope(count, items, error):
    api = Mock()
    if items is None:
        items = [MemoryUnit(id="foreign", scope=Scope(org="outside"))]
    api.list.return_value = MemoryListResult(items, count)
    binding = PersonalScopeBinding(Scope(org="bound"))
    with pytest.raises(error):
        search_personal_records(
            api, [binding], "query", security=object(),
            read_scope=api.list, skip_candidate=lambda _unit: False, top_k=1, source=None,
        )
    api.search.assert_not_called()
