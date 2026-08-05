"""handler 的「身份 / 目标」分离——身份来自认证上下文，目标来自 payload。

本文件原先测的是 ``_actor_scope(payload)``：身份从 ``actor_tenant_id`` /
``actor_scope`` 读、缺省回落成目标 scope。那正是 security.md §9 铁律 #1 要堵的
洞（见 ``tests/integration/test_identity_forgery_rejected.py``），故断言随实现
一并改写。

与集成测试的分工：那边验端到端的授权结果（200/403），这边用 recording API
验**传给 API 边界的 identity 到底是哪个值**——集成测试看不到这一层。
"""

from __future__ import annotations

import importlib
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from common.security.types import AuthContext, Role, reset_current, set_current
from common.type_def import Segment
from common.type_def.scope import Scope

pytestmark = pytest.mark.unit

_BOOTSTRAP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "bootstrap",
    "core",
)
if _BOOTSTRAP not in sys.path:
    sys.path.append(_BOOTSTRAP)

handler = importlib.import_module("handler")

_ALICE = Scope(org="acme", user="alice")


@contextmanager
def _as(actor: Scope):
    """以 ``actor`` 的身份发起请求（等价于中间件认证通过后的状态）。"""
    token = set_current(AuthContext(actor=actor, role=Role.USER))
    try:
        yield
    finally:
        reset_current(token)


class _RecordingApi:
    def __init__(self) -> None:
        self.write_calls = []
        self.recall_calls = []

    def write(
        self,
        content,
        scope,
        modality,
        *,
        identity,
        tags=None,
        assets=None,
        metadata=None,
    ):
        self.write_calls.append({"scope": scope, "identity": identity})
        return [handler.MemoryUnit(id="unit-1", scope=scope, segments=[Segment(content=content)])]

    def recall(self, query, context, *, identity, filters=None, **options):
        self.recall_calls.append(
            {
                "query": query,
                "context": context,
                "identity": identity,
                "filters": filters,
                "options": options,
            }
        )
        return SimpleNamespace(items=[], trajectory=[])


class _RecordingServer:
    def __init__(self) -> None:
        self.api = _RecordingApi()


def _dispatch_add(payload: dict) -> dict:
    srv = _RecordingServer()
    status, body = handler.dispatch(srv, "add", {"content": "hello", **payload})

    assert status == 200, body
    return srv.api.write_calls[0]


def test_identity_comes_from_context_target_from_payload() -> None:
    """同一次请求里两者可以不同：alice 往 owner 的 scope 写。

    能不能写由 PermissionManager 判（这里的 API 是 recording stub，不判）；
    handler 的职责只是**把两个值从各自的来源取对**。
    """
    with _as(_ALICE):
        call = _dispatch_add({"tenant_id": "acme", "space": "product", "scope": "owner"})

    assert call["identity"] == _ALICE
    assert call["scope"] == Scope(org="acme", space="product", user="owner")


def test_identity_is_never_derived_from_target_scope() -> None:
    """payload 完全不给身份线索时，identity 仍是上下文里的那个。

    旧实现在这种情况下让 identity 回落成 target scope——等于「谁访问谁就是主人」。
    """
    with _as(_ALICE):
        call = _dispatch_add({})

    assert call["identity"] == _ALICE
    assert call["scope"] == Scope(org="default", user="")


def test_payload_identity_claims_are_rejected_not_ignored() -> None:
    """``actor_scope`` 这类字段一律 400。

    静默忽略会让调用方以为它仍然生效，写出错误的安全认知。
    """
    srv = _RecordingServer()
    with _as(_ALICE):
        status, body = handler.dispatch(
            srv,
            "add",
            {"content": "hello", "tenant_id": "acme", "scope": "owner", "actor_scope": "auditor"},
        )

    assert status == 400, body
    assert body["error"] == "ValidationError"
    assert "identity must come from credentials" in body["message"]
    assert srv.api.write_calls == []  # 拒在进 API 之前


def test_space_dimension_identity_claims_are_rejected() -> None:
    """``actor_space`` / ``actor_space_id`` 与其余四维同等对待。

    space 是 ``Scope`` 五维化时新加的维度，声明字段每多一维、可冒充的主体就多一维；
    禁止列表若漏了它，伪造面就跟着 ``Scope`` 一起长回来。

    （这条取代了原先断言 ``actor_space`` **能**覆盖 identity 的用例——那个行为
    正是 §9 铁律 #1 要堵的洞。）
    """
    srv = _RecordingServer()
    for key in ("actor_space", "actor_space_id"):
        with _as(_ALICE):
            status, body = handler.dispatch(
                srv, "add", {"content": "hello", "tenant_id": "acme", key: "product"}
            )

        assert status == 400, body
        assert body["error"] == "ValidationError"
        assert key in body["message"]
    assert srv.api.write_calls == []


def test_missing_context_fails_closed() -> None:
    """中间件漏挂 → 401，绝不以某个默认身份跑完。"""
    srv = _RecordingServer()
    status, body = handler.dispatch(srv, "add", {"content": "hello", "tenant_id": "acme"})

    assert status == 401, body
    assert body["error"] == "AuthenticationError"
    assert srv.api.write_calls == []


def test_search_forwards_filter_dsl_to_api_boundary() -> None:
    srv = _RecordingServer()
    filters = {
        "AND": [
            {"metadata.memory_type": "coding"},
            {"OR": [{"project": "alpha"}, {"project": "beta"}]},
        ]
    }

    with _as(_ALICE):
        status, body = handler.dispatch(
            srv,
            "search",
            {"query": "pytest", "tenant_id": "acme", "scope": "alice", "filters": filters},
        )

    assert status == 200, body
    assert srv.api.recall_calls[0]["filters"] == filters
