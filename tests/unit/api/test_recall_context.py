"""search 的 Context 契约：max_tokens 经 Context.extensions 下达自适应披露。"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl import assemble
from jiuwen_memory.api.memory_api_impl.local_support import _recall_permission_context
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def import (
    EXT_MAX_TOKENS,
    EXT_QUERY_CONTEXT,
    Context,
    Modality,
    Scope,
)
from jiuwen_memory.config import Config
from jiuwen_memory.control.application.query import MemoryQueryService
from jiuwen_memory.retrieval.types import DisclosureLevel
from tests.support.scoped_authenticator import ScopedAuthenticator

# discloser 用结构化披露（自适应分级）
_CONFIG = {"discloser": {"default": "structured"}}

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="t", user="u", agent="a", session="s")
_ACTOR = Scope(org="t", user="u")
_TEXT = "alice loves iced americano coffee every single morning before work meetings and calls"


def _api():
    return assemble(config=Config.from_dict(_CONFIG))


def _security(scope: Scope, *, role: Role = Role.USER):
    return internal_context(ScopedAuthenticator(scope, role=role))


def test_context_max_tokens_reaches_adaptive_disclosure() -> None:
    api = _api()
    api.add(
        _TEXT, _SCOPE, source=Modality.TEXT, security=internal_context(ScopedAuthenticator(_ACTOR))
    )

    res = api.search(
        "coffee",
        Context(_SCOPE, extensions={EXT_MAX_TOKENS: "300"}),
        security=internal_context(ScopedAuthenticator(_ACTOR)),
        disclosure=DisclosureLevel.ADAPTIVE,
        with_trajectory=True,
    )

    disclose = next(s for s in res.trajectory if s.stage == "disclose")
    assert disclose.detail["max_tokens"] == "300"


def test_context_without_max_tokens_uses_default() -> None:
    api = _api()
    api.add(
        _TEXT, _SCOPE, source=Modality.TEXT, security=internal_context(ScopedAuthenticator(_ACTOR))
    )

    res = api.search(
        "coffee",
        Context(_SCOPE),  # 不给预算 → max_tokens=None
        security=internal_context(ScopedAuthenticator(_ACTOR)),
        disclosure=DisclosureLevel.ADAPTIVE,
        with_trajectory=True,
    )

    disclose = next(s for s in res.trajectory if s.stage == "disclose")
    assert disclose.detail["max_tokens"] == ""  # 默认策略，无预算


def _capture_recall(monkeypatch):
    captured = []
    original = MemoryQueryService.recall

    async def recall(self, scope, query):
        captured.append(query)
        return await original(self, scope, query)

    monkeypatch.setattr(MemoryQueryService, "recall", recall)
    return captured


def test_search_query_context_reaches_single_space_retrieval(monkeypatch) -> None:
    api = _api()
    captured = _capture_recall(monkeypatch)

    api.search(
        "hello",
        Context(_SCOPE, extensions={EXT_QUERY_CONTEXT: ["用户：hello"]}),
        security=_security(_ACTOR),
    )

    assert captured[0].query_context == ("用户：hello",)


def test_search_query_context_reaches_cross_space_retrieval(monkeypatch) -> None:
    api = _api()
    captured = _capture_recall(monkeypatch)

    api.search(
        "hello",
        Context(_SCOPE, extensions={"spaces": ["s"], EXT_QUERY_CONTEXT: ("用户：hello",)}),
        security=_security(Scope(org="t", user="root"), role=Role.ROOT),
    )

    assert captured[0].query_context == ("用户：hello",)


def test_search_empty_query_context_is_valid_and_not_transferred(monkeypatch) -> None:
    api = _api()
    captured = _capture_recall(monkeypatch)

    api.search(
        "hello",
        Context(_SCOPE, extensions={EXT_QUERY_CONTEXT: []}),
        security=_security(_ACTOR),
    )

    # 空序列表示显式无上下文；该约定 key 不应再作为插件透传项下传。
    assert captured[0].query_context == ()
    assert EXT_QUERY_CONTEXT not in captured[0].extensions


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            None,
            "query_context must not be null; omit the key or provide a list or tuple of strings",
        ),
        ("text", "query_context must be a list or tuple of strings"),
        ({"text": "text"}, "query_context must be a list or tuple of strings"),
        ([1], "query_context items must be strings"),
        ([None], "query_context items must be strings"),
    ],
)
def test_search_rejects_invalid_query_context(raw, message, monkeypatch) -> None:
    api = _api()
    captured = _capture_recall(monkeypatch)

    with pytest.raises(ValidationError, match="^" + message + "$"):
        api.search(
            "hello",
            Context(_SCOPE, extensions={EXT_QUERY_CONTEXT: raw}),
            security=_security(_ACTOR),
        )

    assert captured == [], "invalid query_context must fail before storage recall"


def test_query_context_is_not_authorization_routing_metadata() -> None:
    permission_context = _recall_permission_context(
        Context(_SCOPE, extensions={EXT_QUERY_CONTEXT: ["用户：hello"]}),
        None,
        ("query_context",),
    )

    assert permission_context.metadata == {}, "query_context 不应进入权限路由 metadata"
