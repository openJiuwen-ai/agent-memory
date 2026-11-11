import asyncio
import json

import httpx
import pytest

from jiuwen_memory.api import MemoryRef, MemoryUnit, Scope, Segment
from jiuwen_memory_adapter.continuity import tool_schemas
from jiuwen_memory_adapter.continuity.client import ContinuityClient
from jiuwen_memory_adapter.continuity.history import (
    attach_existing,
    import_history,
    prepare_history,
    rollback_relations,
)
from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import AgentMemoryMemoryProvider
from jiuwen_memory_entry.core.api_contract import parse_request

pytestmark = pytest.mark.unit


def test_remote_authentication_and_unknown_are_not_silent_success():
    def transport(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.url.path == "/v1/continuity_recover"
        return httpx.Response(401, json={"detail": "secret detail not echoed"})

    client = ContinuityClient(
        "https://memory.example", "test-key", transport=httpx.MockTransport(transport)
    )
    assert client.call("continuity_recover", {}) == {"status": "rejected", "error": "http_401"}
    client.close()
    with pytest.raises(ValueError):
        ContinuityClient("http://memory.example", "key")
    with pytest.raises(ValueError):
        ContinuityClient("https://memory.example", "")


def test_history_identity_preflight_and_unknown_boundary(tmp_path):
    source = tmp_path / "history.jsonl"
    source.write_text("\n".join(json.dumps({"content": s}) for s in ["a", "a"]), encoding="utf-8")
    kwargs = dict(
        batch_id="batch",
        task_id="t",
        binding_id="b",
        product="hermes",
        version="test-fixture",
        context_id="session",
    )
    manifest, events = prepare_history(str(source), **kwargs)
    assert events == prepare_history(str(source), **kwargs)[1]
    assert events[0]["event_id"] != events[1]["event_id"], "identical text is not event identity"
    assert manifest["method"] == "user_supplied"

    class Client:
        @staticmethod
        def call(verb, payload):
            assert parse_request(verb, payload)
            return {"outcomes": [{"continuity_status": "unknown", "operation_id": "op"}]}

    result = import_history(Client(), {"org": "o", "space": "p"}, manifest, events)
    assert len(result["results"]) == 1
    assert result["imported"] == 0 and result["remaining"] == len(events)


def test_adapter_schema_includes_nested_fields_and_custom_duty():
    schemas = {s["name"]: s["parameters"] for s in tool_schemas()}
    relation = schemas["agent_memory_continuity_relation"]["properties"]["relation"]
    assert "evidence_refs" in relation["properties"]
    assert "content_digest" in relation["properties"]["source"]["properties"]


def test_adapter_schema_exposes_continuity_recover():
    schemas = {s["name"]: s["parameters"] for s in tool_schemas()}
    rebuild = schemas["agent_memory_continuity_recover"]
    assert rebuild["required"] == ["scope"]
    recovery = rebuild["properties"]["recovery"]
    assert {"type": "null"} in recovery["anyOf"]
    payload = next(v for v in recovery["anyOf"] if v.get("type") == "object")
    assert "kind" in payload["properties"]
    assert "operation_id" in payload["properties"]
    assert "refs" in payload["properties"]


def test_jiuwenswarm_forwards_continuity_recover():
    provider = AgentMemoryMemoryProvider(
        base_url="https://memory.example", api_key="test", continuity_enabled=True
    )

    class Client:
        async def continuity_call(self, verb, payload):
            return {"verb": verb, "scope": payload["scope"], "recovery": payload.get("recovery")}

    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable=protected-access
    provider._client = Client()
    provider._initialized = True
    arguments = {"scope": {"org": "o", "space": "p"}, "recovery": None}
    result = json.loads(
        asyncio.run(provider.handle_tool_call("agent_memory_continuity_recover", arguments))
    )
    assert result["verb"] == "continuity_recover"
    assert result["scope"] == {"org": "o", "space": "p"}


def test_legacy_reference_digest_matches_server_without_reindexing():
    from jiuwen_memory_entry.core.api_contract import to_jsonable

    unit = MemoryUnit(
        id="old", scope=Scope(org="o", user="private"), segments=[Segment(content="legacy")]
    )
    calls = []

    class Client:
        @staticmethod
        def call(verb, payload):
            calls.append((verb, payload))
            return to_jsonable(unit) if verb == "get" else {"revision": 2}

    attach_existing(
        Client(),
        {"org": "o", "space": "p"},
        task_id="t",
        expected_revision=1,
        request_id="attach",
        sources=[{"scope": {"org": "o", "user": "private"}, "unit_id": "old"}],
    )
    assert MemoryRef.parse(calls[-1][1]["request"]["background"][0]) == MemoryRef.of(unit)
    assert [call[0] for call in calls] == ["get", "continuity_task"]


def test_batch_rollback_uses_original_revision_and_cannot_delete_newer_edits():
    relation = {"relation_id": "r", "revision": 2, "batch_id": "b"}

    class Client:
        @staticmethod
        def call(verb, payload):
            if verb == "get":
                return {"segments": [{"content": json.dumps(relation)}]}
            assert payload["expected_revision"] == 2
            assert payload["relation"]["revision"] == 3
            assert payload["relation"]["status"] == "revoked"
            return {"status": "rejected", "error": "http_409"}

    result = rollback_relations(
        Client(), {"org": "o", "space": "p"}, "b", [{"relation_ref": {"scope": {}, "unit_id": "u"}}]
    )
    assert result["results"][0]["error"] == "http_409"


def test_jiuwenswarm_rail_preserves_structured_view_and_durable_receipt():
    from unittest.mock import AsyncMock

    provider = AgentMemoryMemoryProvider(
        base_url="https://memory.example", api_key="test", continuity_enabled=True,
        default_duty=["diagnosis", "execution"],
    )
    client = AsyncMock()
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    provider._client = client
    view = {"paths": [{"valid_duties": []}], "gaps": [{"code": "budget_exhausted"}]}
    client.continuity_call.return_value = view
    scope = {"org": "o", "space": "p"}
    assert json.loads(asyncio.run(provider.prefetch("query", scope=scope, task_id="t"))) == view
    assert client.continuity_call.call_args.args[1]["duty"] == ["diagnosis", "execution"]
    assert client.continuity_call.call_args.args[1]["task_id"] == "t"
    client.continuity_call.return_value = {"status": "unknown", "operation_id": "op"}
    result = asyncio.run(provider.sync_turn(
        "question", "answer", scope=scope, task_id="t", binding_id="b", event_id="turn-1"
    ))
    assert result == {"status": "unknown", "operation_id": "op"}
    assert client.continuity_call.call_args.args[1]["continuity"]["event_id"] == "turn-1"
    with pytest.raises(ValueError, match="stable event ID"):
        asyncio.run(provider.sync_turn("question", "answer", scope=scope))
    client.continuity_call.side_effect = ConnectionError("offline")
    with pytest.raises(ConnectionError):
        asyncio.run(provider.prefetch("query", scope=scope))
    asyncio.run(provider.on_session_end())
    client.evolve_extract.assert_not_called()
