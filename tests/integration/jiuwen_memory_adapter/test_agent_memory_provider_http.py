# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentMemoryMemoryProvider HTTP 模式 × 真实 HttpServer（offline profile + dev auth）。

契约依据：``jiuwen_memory_adapter/jiuwenswarm/jiuwen_memory_rail_design.md`` §4.1/§8.2。
用真实服务做 ground truth——旧版 HTTP 客户端正是按想象的契约（legacy ``{ok, op}``
信封 + 扁平 scope + ``k`` 别名）编写而对当前服务端全量 400；本文件逐条锁死
api_contract 表面的关键行为。

部署形态：jiuwenswarm 宿主把 ``jiuwen_memory_adapter/jiuwenswarm`` 放进
PYTHONPATH，provider 以**顶层模块**被 import（``agent_memory_provider``）——测试
按同一形态装配 sys.path，保证被测的就是部署时执行的那份模块身份。

``jiuwen_memory_entry/http_server`` 依赖 core 目录的 flat import（``import server``
等）且只把 core **追加**到 sys.path 尾部——本机 site-packages 预置了同名 ``server``
目录时会抢先命中（现有 ``tests/integration/jiuwen_memory_entry`` 在本机同样受影响，
CI 无此包不受影响）。故这里把 core 目录**前置**，保证 flat import 命中正确模块。
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from queue import Queue

import pytest
import pytest_asyncio

# 本文件 -> tests/integration/jiuwen_memory_adapter -> tests/integration -> tests -> 仓库根
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADAPTER_DIR = _REPO_ROOT / "jiuwen_memory_adapter" / "jiuwenswarm"
_ENTRY_CORE_DIR = _REPO_ROOT / "jiuwen_memory_entry" / "core"
for _dir in (_ADAPTER_DIR, _ENTRY_CORE_DIR):
    if _dir.is_dir() and str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

# ruff: noqa: E402
from agent_memory_provider import AgentMemoryMemoryProvider

from jiuwen_memory_entry.core.dev_security import with_local_dev_security
from jiuwen_memory_entry.core.profiles import OFFLINE, load_config
from jiuwen_memory_entry.http_server import __main__ as http_server_module
from jiuwen_memory_entry.http_server.__main__ import HttpServer

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENVELOPE_ZH = '你收到一条消息：\n{"source": "tui", "content": "记住常用语言是 Python"}'


@pytest.fixture(name="memory_http_url", scope="module")
def memory_http_url_fixture():
    """起真实 HttpServer（offline 内核 + dev 认证 allow_all），port 0 随机。"""
    ready: Queue[ThreadingHTTPServer] = Queue()

    class _ReadyHttpd(ThreadingHTTPServer):
        def serve_forever(self, poll_interval: float = 0.05) -> None:
            ready.put(self)
            super().serve_forever(poll_interval=poll_interval)

    original_cls = http_server_module.ThreadingHTTPServer
    http_server_module.ThreadingHTTPServer = _ReadyHttpd
    config = load_config([OFFLINE])
    server = HttpServer.build(with_local_dev_security(config))
    with ThreadPoolExecutor(max_workers=1) as executor:
        serving = executor.submit(server.serve, "127.0.0.1", 0)
        httpd = None
        try:
            httpd = ready.get(timeout=10)
            yield f"http://127.0.0.1:{httpd.server_port}"
        finally:
            http_server_module.ThreadingHTTPServer = original_cls
            if httpd is not None:
                httpd.shutdown()
            serving.result(timeout=10)
            server.close(wait=True)


@pytest_asyncio.fixture(name="make_provider")
async def make_provider_fixture(memory_http_url):
    """按测试隔离需要构造 provider（每个测试用独立 user_id 隔离写入命名空间）。"""
    made: list[AgentMemoryMemoryProvider] = []

    async def _make(user_id: str) -> AgentMemoryMemoryProvider:
        provider = AgentMemoryMemoryProvider(base_url=memory_http_url)
        await provider.initialize(user_id=user_id, scope_id="tenant-http", session_id="")
        made.append(provider)
        return provider

    yield _make
    for provider in made:
        await provider.shutdown()


# --------------------------------------------------------------------------- #
# 契约：add 顶层数组 + segments 重建 content
# --------------------------------------------------------------------------- #


async def test_add_returns_result_with_content_rebuilt_from_segments(make_provider):
    provider = await make_provider("user-add-rebuild")
    client = provider._client
    result = await client.add(
        "契约测试：add 响应无顶层 content，需从 segments 重建",
        provider.bound_scope(),
        tags=["contract"],
    )
    assert result is not None
    assert result.item_id
    assert result.content == "契约测试：add 响应无顶层 content，需从 segments 重建"
    # offline 栈直写路径（无 infer）默认 EPISODIC——design doc §8.2.1 探针 2b 已实测
    assert result.tier == "episodic"


async def test_conclude_then_search_tool_roundtrip(make_provider):
    provider = await make_provider("user-roundtrip")
    result = json.loads(
        await provider.handle_tool_call("agent_memory_conclude", {"conclusion": "用户偏好深色主题"})
    )
    assert result["result"] == "Fact stored."
    assert result["item_id"]

    result = json.loads(
        await provider.handle_tool_call("agent_memory_search", {"query": "深色主题"})
    )
    assert result["count"] >= 1
    assert any("深色主题" in item["memory"] for item in result["results"])


async def test_unregistered_profile_tool_still_callable(make_provider):
    """未注册 ≠ 删除：profile 不在 get_tool_schemas（LLM 不可见），但
    handle_tool_call 直调仍走真实实现（list 经 /v1/list 闭环）。"""
    provider = await make_provider("user-profile")
    registered = [s["name"] for s in provider.get_tool_schemas()]
    assert "agent_memory_profile" not in registered

    await provider.handle_tool_call(
        "agent_memory_conclude", {"conclusion": "profile 工具应能列出本条"}
    )
    result = json.loads(await provider.handle_tool_call("agent_memory_profile", {}))
    assert "profile 工具应能列出本条" in result["result"]


async def test_read_md_file_client_error_propagates(make_provider):
    """offline 栈非文档模式：read_md_file 被服务端 400 拒绝，错误经
    AgentMemoryClientError 透出（区别于传输失败降级的 unavailable）。"""
    provider = await make_provider("user-md-reject")
    result = json.loads(
        await provider.handle_tool_call(
            "agent_memory_read_md_file", {"md_filename": "MEMORY.md"}
        )
    )
    assert "rejected" in result["error"]
    assert "unavailable" not in result["error"]


# --------------------------------------------------------------------------- #
# §6.2-F：prefetch 召回口径（tier 过滤服务端真实执行）
# --------------------------------------------------------------------------- #


async def test_prefetch_excludes_episodic_originals_while_search_tool_finds_them(
    make_provider,
):
    provider = await make_provider("user-tier-filter")
    # conclude 走直写路径 → episodic 原文（建索引）
    await provider.handle_tool_call(
        "agent_memory_conclude", {"conclusion": "唯一的深色主题偏好锚点"}
    )

    # prefetch 只召回 semantic/core/procedural → episodic 原文被排除
    prefetch = await provider.prefetch("深色主题偏好")
    assert "深色主题" not in prefetch

    # search 工具不过滤 tier → 命中原文
    result = json.loads(
        await provider.handle_tool_call("agent_memory_search", {"query": "深色主题偏好锚点"})
    )
    assert any("深色主题" in item["memory"] for item in result["results"])


# --------------------------------------------------------------------------- #
# §6.2-E：sync_turn 剥 TUI 信封（检索 query 与落盘内容形态一致）
# --------------------------------------------------------------------------- #


async def test_sync_turn_strips_envelope_before_storing(make_provider):
    provider = await make_provider("user-envelope")
    await provider.sync_turn(ENVELOPE_ZH, "好的，已记住。")

    # 显式检索（无 tier 过滤）命中后，内容不得包含信封壳
    result = json.loads(
        await provider.handle_tool_call("agent_memory_search", {"query": "常用语言"})
    )
    assert result["count"] >= 1
    for item in result["results"]:
        assert "你收到一条消息" not in item["memory"]
    assert any("Python" in item["memory"] for item in result["results"])


# --------------------------------------------------------------------------- #
# §6.2-D：身份覆盖式 scope（dev 认证 local/developer 写读闭环）
# --------------------------------------------------------------------------- #


async def test_identity_covering_scope_write_read_roundtrip(make_provider):
    provider = await make_provider("user-identity-scope")
    client = provider._client
    payload = client._scope_payload(provider.bound_scope())
    assert payload["org"] == "local"
    assert payload["user"] == "developer"
    assert payload["session"] == "user-identity-scope"

    result = await client.add("身份覆盖式 scope 写入", provider.bound_scope())
    assert result is not None and result.item_id
    hits = await client.search("身份覆盖式", provider.bound_scope(), top_k=5)
    assert any("身份覆盖式" in hit["content"] for hit in hits)


# --------------------------------------------------------------------------- #
# evolve（裸 job_id）与旧契约拒收（回归锚）
# --------------------------------------------------------------------------- #


async def test_evolve_extract_returns_job_id(make_provider):
    provider = await make_provider("user-evolve")
    client = provider._client
    payload = {"scope": client._scope_payload(provider.bound_scope()), "mode": "extract"}
    job_id = await client._post_verb("evolve", payload, write=True)
    assert isinstance(job_id, str) and job_id


async def test_legacy_flat_payload_is_rejected_by_server(make_provider):
    """回归锚：旧版客户端的扁平 payload（k / tenant_id）被服务端 400 拒收。"""
    provider = await make_provider("user-legacy-reject")
    client = provider._client
    legacy_payload = {
        "query": "q",
        "k": 5,
        "tenant_id": "tenant-http",
        "scope": "user-legacy-reject",
    }
    assert await client._post_verb("search", legacy_payload) is None


# --------------------------------------------------------------------------- #
# §6.2-C：熔断器
# --------------------------------------------------------------------------- #


async def test_breaker_opens_after_consecutive_failures(make_provider):
    provider = await make_provider("user-breaker")
    client = provider._client
    for _ in range(5):
        assert await client._post_verb("no_such_verb", {}) is None

    assert client.is_unavailable() is True

    # 熔断开启：工具调用立即返回暂不可用，不再触网
    result = json.loads(await provider.handle_tool_call("agent_memory_search", {"query": "x"}))
    assert "unavailable" in result["error"]

    # 读路径同样短路降级（不抛错）
    assert await client.search("x", provider.bound_scope()) == []
