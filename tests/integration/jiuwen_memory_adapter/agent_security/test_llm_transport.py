"""真实 HTTP transport 的最终参数、凭据配置和重定向边界。"""

import asyncio
import hashlib
from urllib.parse import urljoin

import pytest

from jiuwen_memory_adapter.agent_security import Actor, ApprovalChoice, LlmTarget, SecurityDenied
from jiuwen_memory_adapter.agent_security.llm import LlmCallContext, create_llm_adapter
from jiuwen_memory_adapter.agent_security.sources import SourceLedger

from .fixtures import SAMPLE, llm_guard

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "mode",
    ["valid", "header", "metadata", "credential", "profile", "duplicate", "redirect", "mutation"],
)
def test_final_transport_contract(llm_server, mode):
    root, server = llm_server
    actor = Actor("tenant", "alice", "transport", "agent", "1")
    sources = SourceLedger()
    sources.begin(actor, fresh=True)
    guard, shown, captured = llm_guard({"a", "b"}), [], {}

    def present(public):
        shown.append(public)
        if mode == "mutation":
            # The caller retains a mutable HTTPX Request while approval is outstanding.
            original = captured["request"]
            original.url = type(original.url)(urljoin(root, "/b/v1/chat/completions"))
            original.headers["authorization"] = "Bearer changed-after-check"
        guard.broker.submit(
            public["approval_id"],
            public["version"],
            ApprovalChoice.SEND_REDACTED,
            reviewer=actor,
        )

    adapter = create_llm_adapter(
        {"framework": "openjiuwen"},
        guard,
        sources=sources,
        targets=[
            LlmTarget("a", "provider-a", "model-a", urljoin(root, "/a/v1/chat/completions")),
            LlmTarget("b", "provider-b", "model-b", urljoin(root, "/b/v1/chat/completions")),
        ],
        context_for=lambda _: LlmCallContext(actor, "wire-contract"),
        present=present,
    )
    headers = {"authorization": "Bearer fixture-auth"}
    if mode == "credential":
        headers["authorization"] = "Bearer different-auth"
    if mode == "header":
        headers["x-memory-content"] = SAMPLE
    if mode == "metadata":
        headers["user-agent"] = SAMPLE
    path = "/b/" if mode == "profile" else "/a/"
    body = {
        "model": "model-b" if mode == "profile" else "model-a",
        "messages": [{"role": "user", "content": SAMPLE}],
    }
    if mode == "redirect":
        server["redirects"]["/a/v1/chat/completions"] = urljoin(root, "/b/v1/chat/completions")

    def request(client):
        kwargs = {"headers": headers, "json": body}
        if mode == "duplicate":
            kwargs.pop("json")
            kwargs["content"] = '{"model":"model-a","model":"model-b","messages":[]}'
            headers["content-type"] = "application/json"
        value = client.build_request("POST", root + path + "v1/chat/completions", **kwargs)
        captured["request"] = value
        return value

    options = {
        "auth_headers": {"authorization": "Bearer fixture-auth"},
        "target_ids": {"a"},
        "follow_redirects": True,
    }
    succeeds = mode in {"valid", "mutation"}

    async def run_async():
        async with adapter.create_http_client(**options) as client:
            with adapter.scope(object()):
                if succeeds:
                    assert (await client.send(request(client))).status_code == 200
                else:
                    with pytest.raises(SecurityDenied):
                        await client.send(request(client))

    try:
        asyncio.run(run_async())
        assert len(server["calls"]) == (1 if succeeds or mode == "redirect" else 0)
        assert len(shown) == (1 if succeeds or mode == "redirect" else 0)
        if server["calls"]:
            assert server["calls"][0]["path"] == "/a/v1/chat/completions"
            assert server["calls"][0]["body"]["messages"][0]["content"] == "[PRIVATE]"
            assert (
                server["calls"][0]["auth_digest"]
                == hashlib.sha256(b"Bearer fixture-auth").hexdigest()
            )
    finally:
        adapter.close()
        sources.close()
        guard.broker.close()


def test_sdk_retry_rechecks_current_authorization(llm_server):
    from openai import AsyncOpenAI

    root, server = llm_server
    server["failures"]["/a/v1/chat/completions"] = 1
    actor = Actor("tenant", "alice", "retry", "agent", "1")
    sources, shown, allowed = SourceLedger(), [], {"value": True}
    sources.begin(actor, fresh=True)
    guard = llm_guard({"a"})

    def authorize(_):
        return allowed["value"]

    guard.authorize = authorize

    def present(public):
        shown.append(public)
        guard.broker.submit(
            public["approval_id"],
            public["version"],
            ApprovalChoice.SEND_REDACTED,
            reviewer=actor,
        )

    adapter = create_llm_adapter(
        {"framework": "openjiuwen"},
        guard,
        sources=sources,
        targets=[LlmTarget("a", "provider-a", "model-a", urljoin(root, "/a/v1/chat/completions"))],
        context_for=lambda _: LlmCallContext(actor, "retry-revocation"),
        present=present,
    )

    def revoke(response):
        assert response.status_code == 503
        allowed["value"] = False

    async def arevoke(response):
        revoke(response)

    options = {
        "auth_headers": {"authorization": "Bearer fixture-auth"},
        "target_ids": {"a"},
        "event_hooks": {"response": [arevoke]},
    }
    params = {"model": "model-a", "messages": [{"role": "user", "content": SAMPLE}]}
    sdk = {"api_key": "fixture-auth", "base_url": urljoin(root, "/a/v1"), "max_retries": 1}

    async def run_async():
        async with AsyncOpenAI(http_client=adapter.create_http_client(**options), **sdk) as client:
            with adapter.scope(object()), pytest.raises(Exception):
                await client.chat.completions.create(**params)

    try:
        asyncio.run(run_async())
        assert len(server["calls"]) == len(shown) == 1
        assert not allowed["value"]
    finally:
        adapter.close()
        sources.close()
        guard.broker.close()
