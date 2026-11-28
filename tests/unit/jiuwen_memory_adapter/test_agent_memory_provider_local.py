"""JiuwenSwarm provider local connection (F17 Task 16)."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import asyncio
import json

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import (
    AgentMemoryMemoryProvider,
    _LocalClient,
)

pytestmark = pytest.mark.unit


class FakeService:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.leased = 0
        self.released = 0
        self.response = (200, {"view": {"items": [{"unit_id": "u1"}]}})

    def post(self, path, payload, **kwargs):
        self.calls.append((path, payload))
        return self.response

    def lease(self, install_id):
        self.leased += 1

    def release(self, install_id):
        self.released += 1


def _client():
    client = _LocalClient("codex-1")
    client._service = FakeService()
    client._is_initialized = True
    return client


def test_scope_payload_is_the_sdk_five_dimension_mapping():
    client = _LocalClient("codex-1")
    scope = Scope(org="local", space="proj", user="u1", agent="a1", session="s1")
    assert client._scope_payload(scope) == {
        "org": "local",
        "space": "proj",
        "user": "u1",
        "agent": "a1",
        "session": "s1",
    }


def test_prefetch_sends_project_duty_and_top_k():
    client = _client()
    text = asyncio.run(
        client.prefetch_local(
            "why", project="proj", duty=["diagnosis"], top_k=4, event_id="t1",
            session_id="s1",
        )
    )
    path, payload = client._service.calls[0]
    assert path == "/local/hook/recall"
    assert payload["project"] == "proj"
    assert payload["duty"] == ["diagnosis"]
    assert payload["top_k"] == 4
    assert payload["turn_id"] == "t1"
    assert json.loads(text)["items"] == [{"unit_id": "u1"}]


def test_generated_turn_id_is_reused_by_sync_turn():
    client = _client()
    asyncio.run(
        client.prefetch_local("q", project="p", duty="general", top_k=10, session_id="s1")
    )
    turn_id = client._turn_id
    assert turn_id
    asyncio.run(client.sync_turn_local("user", "assistant", session_id="s1"))
    path, payload = client._service.calls[-1]
    assert path == "/local/hook/capture"
    assert payload["turn_id"] == turn_id
    assert client._turn_id == ""


def test_sync_turn_without_turn_id_does_not_write():
    client = _client()
    assert asyncio.run(client.sync_turn_local("u", "a", session_id="s1")) is None
    assert client._service.calls == []


def test_explicit_event_id_wins():
    client = _client()
    asyncio.run(
        client.prefetch_local("q", project="p", duty="general", top_k=10, event_id="evt")
    )
    asyncio.run(client.sync_turn_local("u", "a", event_id="other"))
    assert client._service.calls[-1][1]["turn_id"] == "other"


def test_service_error_degrades_to_an_empty_view():
    client = _client()
    client._service.response = (503, {})
    text = asyncio.run(client.prefetch_local("q", project="p", duty="general", top_k=10))
    view = json.loads(text)
    assert view["items"] == []
    assert view["gaps"][0]["code"] == "local_unavailable"


def test_lease_and_release_go_through_the_service():
    client = _client()
    client.lease()
    client.release()
    assert (client._service.leased, client._service.released) == (1, 1)


def test_local_connection_requires_install_id():
    with pytest.raises(ValueError):
        AgentMemoryMemoryProvider(connection="local")
    with pytest.raises(ValueError):
        AgentMemoryMemoryProvider(connection="local", install_id="i", base_url="http://x")
    with pytest.raises(ValueError):
        AgentMemoryMemoryProvider(connection="telnet")


class FakeLocal:
    def __init__(self):
        self.prefetch_kwargs = {}
        self.sync_kwargs = {}
        self.released = False
        self.closed = False

    async def prefetch_local(self, query, **kwargs):
        self.prefetch_kwargs = {"query": query, **kwargs}
        return json.dumps({"items": []})

    async def sync_turn_local(self, user_msg, assistant_msg, **kwargs):
        self.sync_kwargs = {"user": user_msg, "assistant": assistant_msg, **kwargs}
        return {"status": "committed"}

    def release(self):
        self.released = True

    async def close(self):
        self.closed = True


def _provider():
    provider = AgentMemoryMemoryProvider(connection="local", install_id="codex-1")
    provider._client = FakeLocal()
    provider._initialized = True
    provider._session_id = "s1"
    return provider


def test_provider_prefetch_delegates_project_duty_and_top_k():
    provider = _provider()
    asyncio.run(
        provider.prefetch(
            "q", scope={"space": "proj"}, duty=["diagnosis"], top_k=3, event_id="t9"
        )
    )
    kwargs = provider._client.prefetch_kwargs
    assert kwargs["project"] == "proj"
    assert kwargs["duty"] == ["diagnosis"]
    assert kwargs["top_k"] == 3
    assert kwargs["event_id"] == "t9"
    assert kwargs["session_id"] == "s1"


def test_provider_sync_turn_delegates_and_shutdown_releases():
    provider = _provider()
    assert asyncio.run(provider.sync_turn("user", "assistant", event_id="t1")) == {
        "status": "committed"
    }
    assert provider._client.sync_kwargs["event_id"] == "t1"
    fake = provider._client
    asyncio.run(provider.shutdown())
    assert fake.released is True, "shutdown must release the lease"
    assert fake.closed is True, "shutdown must close the client"
    assert provider._client is None


def test_non_boolean_portal_option_is_rejected():
    with pytest.raises(ValueError):
        AgentMemoryMemoryProvider(portal={"enabled": "yes"})
