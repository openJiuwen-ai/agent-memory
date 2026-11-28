"""MCP bridge: self-binding allowed, cross-connection blocked, receipts preserved."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from jiuwen_memory_adapter.continuity import tool_schemas
from jiuwen_memory_adapter.continuity.client import ContinuityClient
from jiuwen_memory_adapter.continuity.mcp import _check_task_operation, schemas_for

pytestmark = pytest.mark.unit

LOCAL = SimpleNamespace(connector_id="mine")


class FakeClient:
    def __init__(self, bindings=None):
        self.bindings = bindings or []
        self.calls = []

    def call(self, verb, payload):
        self.calls.append((verb, payload))
        return {"bindings": self.bindings}


def _run(arguments, bindings=None):
    return asyncio.run(
        _check_task_operation(FakeClient(bindings), LOCAL, arguments)
    )


def test_agent_may_bind_its_own_connection():
    _run({"scope": {"org": "o", "space": "p"},
          "request": {"operation": "bind", "connector_id": "mine"}})


def test_agent_may_not_bind_another_connection():
    with pytest.raises(ValueError):
        _run({"scope": {"org": "o", "space": "p"},
              "request": {"operation": "bind", "connector_id": "other"}})


def test_agent_may_unbind_its_own_binding():
    _run(
        {"scope": {"org": "o", "space": "p"},
         "request": {"operation": "unbind", "task_id": "t", "binding_id": "b1"}},
        bindings=[{"binding_id": "b1", "connector_id": "mine"}],
    )


def test_agent_may_not_unbind_another_binding():
    with pytest.raises(ValueError):
        _run(
            {"scope": {"org": "o", "space": "p"},
             "request": {"operation": "unbind", "task_id": "t", "binding_id": "b2"}},
            bindings=[{"binding_id": "b2", "connector_id": "other"}],
        )


def test_close_and_attach_stay_admin_only():
    for operation in ("close", "attach"):
        with pytest.raises(ValueError):
            _run({"scope": {"org": "o", "space": "p"},
                  "request": {"operation": operation, "task_id": "t"}})


def test_tool_schemas_include_add_and_search():
    names = {schema["name"] for schema in tool_schemas(include_io=True)}
    assert {"agent_memory_add", "agent_memory_search"} <= names
    base = {schema["name"] for schema in tool_schemas()}
    assert "agent_memory_add" not in base


def _client(handler):
    return ContinuityClient(
        "http://127.0.0.1:1", "token", transport=httpx.MockTransport(handler)
    )


def test_409_receipt_fields_are_passed_through():
    body = {
        "error": "WriteIncompleteError",
        "status": "partial",
        "operation_id": "op-1",
        "refs": [{"unit_id": "u1"}],
        "retry_action": "retry_original_write",
    }
    client = _client(lambda request: httpx.Response(409, json=body))
    result = client.call("add", {"content": "x", "scope": {}})
    assert result["status"] == "partial"
    assert result["operation_id"] == "op-1"
    assert result["refs"] == [{"unit_id": "u1"}]
    assert result["retry_action"] == "retry_original_write"


def test_conflict_without_receipt_stays_rejected():
    client = _client(lambda request: httpx.Response(409, json={"error": "ConflictError"}))
    assert client.call("continuity_task", {})["status"] == "rejected"


def test_add_accepts_a_top_level_array_response():
    client = _client(lambda request: httpx.Response(200, json=[{"id": "u1"}]))
    assert client.call("add", {"content": "x"}) == [{"id": "u1"}]


def test_remote_mode_tool_list_is_unchanged():
    remote = {schema["name"] for schema in schemas_for("remote")}
    local = {schema["name"] for schema in schemas_for("local")}
    assert "agent_memory_add" not in remote and "agent_memory_search" not in remote
    assert {"agent_memory_add", "agent_memory_search"} <= local
    assert remote == {schema["name"] for schema in tool_schemas()}


def test_unbind_lookup_failure_says_lookup_failed():
    class BrokenClient:
        @staticmethod
        def call(verb, payload):
            return {"status": "unknown", "error": "transport_unavailable"}

    with pytest.raises(ValueError, match="lookup failed"):
        asyncio.run(
            _check_task_operation(
                BrokenClient(),
                LOCAL,
                {"scope": {"org": "o", "space": "p"},
                 "request": {"operation": "unbind", "task_id": "t", "binding_id": "b1"}},
            )
        )


def test_main_guard_is_the_last_statement():
    """The bridge must define every used function before asyncio.run blocks the module."""
    source = Path(__file__).parents[4] / "jiuwen_memory_adapter/continuity/mcp.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    last = tree.body[-1]
    assert isinstance(last, ast.If), "python -m would block before later definitions"
    test = last.test
    assert isinstance(test, ast.Compare)
    assert isinstance(test.left, ast.Name) and test.left.id == "__name__"
