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

异步驱动：不依赖 ``pytest-asyncio``——参照 ``test_jiuwenswarm_access.py`` 用
``asyncio.run`` 驱动。HTTP 客户端的 ``httpx.AsyncClient`` 绑定创建时的事件循环，
故每个测试在**同一个** ``asyncio.run`` 内完成 ``initialize → 操作 → shutdown`` 全
生命周期（不能跨多个 ``asyncio.run`` 复用同一 provider）；``_scoped_provider``
async 上下文管理器封装该生命周期，在 ``asyncio.run`` 内 ``async with`` 使用。
"""

# 契约测试直取 provider/client 受保护成员（_client / _scope_payload / _post_verb）
# 以锁死 HTTP 表面行为。
# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from queue import Queue

import pytest

# 本文件 -> tests/integration/jiuwen_memory_adapter -> tests/integration -> tests -> 仓库根
# append（而非 insert(0)）：避免模块重名告警；仓库内 flat import 的 server 等模块
# 在 CI/codecheck 环境无 site-packages 同名包冲突，尾部追加即命中（参照
# test_identity_forgery_rejected.py / test_dev_fallback_business_continuity.py 同样
# 加载 entry core 目录均用 append）。本机若有同名 server 包污染，见文件头注释。
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADAPTER_DIR = _REPO_ROOT / "jiuwen_memory_adapter" / "jiuwenswarm"
_ENTRY_CORE_DIR = _REPO_ROOT / "jiuwen_memory_entry" / "core"
for _dir in (_ADAPTER_DIR, _ENTRY_CORE_DIR):
    if _dir.is_dir() and str(_dir) not in sys.path:
        sys.path.append(str(_dir))

# ruff: noqa: E402
from agent_memory_provider import AgentMemoryMemoryProvider

from jiuwen_memory_entry.core.dev_security import with_local_dev_security
from jiuwen_memory_entry.core.profiles import OFFLINE, load_config
from jiuwen_memory_entry.http_server import __main__ as http_server_module
from jiuwen_memory_entry.http_server.__main__ import HttpServer

pytestmark = pytest.mark.integration

ENVELOPE_ZH = '你收到一条消息：\n{"source": "tui", "content": "记住常用语言是 Python"}'


@pytest.fixture(name="memory_http_url", scope="module")
def memory_http_url_fixture(request, tmp_path_factory):
    """起真实 HttpServer（offline 内核 + dev 认证 allow_all），port 0 随机。"""
    ready: Queue[ThreadingHTTPServer] = Queue()

    class _ReadyHttpd(ThreadingHTTPServer):
        def serve_forever(self, poll_interval: float = 0.05) -> None:
            ready.put(self)
            super().serve_forever(poll_interval=poll_interval)

    original_cls = http_server_module.ThreadingHTTPServer
    http_server_module.ThreadingHTTPServer = _ReadyHttpd
    layers = [OFFLINE]
    if getattr(request, "param", None) == "document":
        from tests.unit.api.test_document_security_lifecycle import _doc_config

        layers.append({"memory_api": _doc_config(
            tmp_path_factory.mktemp("code-http-memory"), watch=False)})
    config = load_config(layers)
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


@contextlib.asynccontextmanager
async def _scoped_provider(memory_http_url: str, user_id: str):
    """构造并初始化 provider，用完 shutdown（保证 ``httpx.AsyncClient`` 与当前事件循环绑定）。

    每个测试在 ``asyncio.run`` 内 ``async with`` 使用：``initialize → 操作 → shutdown``
    全程同一事件循环，避免 ``AsyncClient`` 跨循环失效。``user_id`` 隔离写入命名空间。
    """
    provider = AgentMemoryMemoryProvider(base_url=memory_http_url)
    await provider.initialize(user_id=user_id, scope_id="tenant-http", session_id="")
    try:
        yield provider
    finally:
        await provider.shutdown()


# --------------------------------------------------------------------------- #
# 契约：add 顶层数组 + segments 重建 content
# --------------------------------------------------------------------------- #


def test_add_returns_result_with_content_rebuilt_from_segments(memory_http_url):
    async def _run() -> None:
        async with _scoped_provider(memory_http_url, "user-add-rebuild") as provider:
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

    asyncio.run(_run())


def test_conclude_then_search_tool_roundtrip(memory_http_url):
    async def _run() -> None:
        async with _scoped_provider(memory_http_url, "user-roundtrip") as provider:
            result = json.loads(
                await provider.handle_tool_call(
                    "agent_memory_conclude", {"conclusion": "用户偏好深色主题"}
                )
            )
            assert result["result"] == "Fact stored."
            assert result["item_id"]

            result = json.loads(
                await provider.handle_tool_call("agent_memory_search", {"query": "深色主题"})
            )
            assert result["count"] >= 1
            assert any("深色主题" in item["memory"] for item in result["results"])

    asyncio.run(_run())


def test_unregistered_profile_tool_still_callable(memory_http_url):
    """未注册 ≠ 删除：profile 不在 get_tool_schemas（LLM 不可见）。

    但 handle_tool_call 直调仍走真实实现（list 经 /v1/list 闭环）。
    """

    async def _run() -> None:
        async with _scoped_provider(memory_http_url, "user-profile") as provider:
            registered = [s["name"] for s in provider.get_tool_schemas()]
            assert "agent_memory_profile" not in registered

            await provider.handle_tool_call(
                "agent_memory_conclude", {"conclusion": "profile 工具应能列出本条"}
            )
            result = json.loads(await provider.handle_tool_call("agent_memory_profile", {}))
            assert "profile 工具应能列出本条" in result["result"]

    asyncio.run(_run())


def test_read_md_file_client_error_propagates(memory_http_url):
    """offline 栈非文档模式：read_md_file 被服务端 400 拒绝。

    错误经 AgentMemoryClientError 透出（区别于传输失败降级的 unavailable）。
    """

    async def _run() -> None:
        async with _scoped_provider(memory_http_url, "user-md-reject") as provider:
            result = json.loads(
                await provider.handle_tool_call(
                    "agent_memory_read_md_file", {"md_filename": "MEMORY.md"}
                )
            )
            assert "rejected" in result["error"]
            assert "unavailable" not in result["error"]

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# §6.2-F：prefetch 召回口径（tier 过滤服务端真实执行）
# --------------------------------------------------------------------------- #


def test_prefetch_excludes_episodic_originals_while_search_tool_finds_them(memory_http_url):
    async def _run() -> None:
        async with _scoped_provider(memory_http_url, "user-tier-filter") as provider:
            client = provider._client
            # 直写路径（无 infer）→ episodic 原文（建索引）。conclude 走 infer=true 会经
            # keyword 抽取产出 semantic 派生单元（§6.2-F 收进来），测不出 tier 过滤效果。
            added = await client.add(
                "唯一的深色主题偏好锚点",
                provider.bound_scope(),
                tags=["tier-filter"],
            )
            assert added is not None and added.tier == "episodic"

            # prefetch 只召回 semantic/core/procedural → episodic 原文被排除
            prefetch = await provider.prefetch("深色主题偏好")
            assert "深色主题" not in prefetch

            # search 工具不过滤 tier → 命中原文
            result = json.loads(
                await provider.handle_tool_call(
                    "agent_memory_search", {"query": "深色主题偏好锚点"}
                )
            )
            assert any("深色主题" in item["memory"] for item in result["results"])

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# §6.2-E：sync_turn 剥 TUI 信封（检索 query 与落盘内容形态一致）
# --------------------------------------------------------------------------- #


def test_sync_turn_strips_envelope_before_storing(memory_http_url):
    async def _run() -> None:
        async with _scoped_provider(memory_http_url, "user-envelope") as provider:
            await provider.sync_turn(ENVELOPE_ZH, "好的，已记住。")

            # 显式检索（无 tier 过滤）命中后，内容不得包含信封壳
            result = json.loads(
                await provider.handle_tool_call("agent_memory_search", {"query": "常用语言"})
            )
            assert result["count"] >= 1
            for item in result["results"]:
                assert "你收到一条消息" not in item["memory"]
            assert any("Python" in item["memory"] for item in result["results"])

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# §6.2-D：身份覆盖式 scope（dev 认证 local/developer 写读闭环）
# --------------------------------------------------------------------------- #


def test_identity_covering_scope_write_read_roundtrip(memory_http_url):
    async def _run() -> None:
        async with _scoped_provider(memory_http_url, "user-identity-scope") as provider:
            client = provider._client
            payload = client._scope_payload(provider.bound_scope())
            assert payload["org"] == "local"
            assert payload["user"] == "developer"
            assert payload["session"] == "user-identity-scope"

            result = await client.add("身份覆盖式 scope 写入", provider.bound_scope())
            assert result is not None and result.item_id
            hits = await client.search("身份覆盖式", provider.bound_scope(), top_k=5)
            assert any("身份覆盖式" in hit["content"] for hit in hits)

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# evolve（裸 job_id）与旧契约拒收（回归锚）
# --------------------------------------------------------------------------- #


def test_evolve_extract_returns_job_id(memory_http_url):
    async def _run() -> None:
        async with _scoped_provider(memory_http_url, "user-evolve") as provider:
            client = provider._client
            payload = {
                "scope": client._scope_payload(provider.bound_scope()),
                "mode": "extract",
            }
            job_id = await client._post_verb("evolve", payload, write=True)
            assert isinstance(job_id, str) and job_id

    asyncio.run(_run())


def test_legacy_flat_payload_is_rejected_by_server(memory_http_url):
    """回归锚：旧版客户端的扁平 payload（k / tenant_id）被服务端 400 拒收。"""

    async def _run() -> None:
        async with _scoped_provider(memory_http_url, "user-legacy-reject") as provider:
            client = provider._client
            legacy_payload = {
                "query": "q",
                "k": 5,
                "tenant_id": "tenant-http",
                "scope": "user-legacy-reject",
            }
            assert await client._post_verb("search", legacy_payload) is None

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# §6.2-C：熔断器
# --------------------------------------------------------------------------- #


def test_breaker_opens_after_consecutive_failures(memory_http_url):
    async def _run() -> None:
        async with _scoped_provider(memory_http_url, "user-breaker") as provider:
            client = provider._client
            for _ in range(5):
                assert await client._post_verb("no_such_verb", {}) is None

            assert client.is_unavailable() is True

            # 熔断开启：工具调用立即返回暂不可用，不再触网
            result = json.loads(
                await provider.handle_tool_call("agent_memory_search", {"query": "x"})
            )
            assert "unavailable" in result["error"]

            # 读路径同样短路降级（不抛错）
            assert await client.search("x", provider.bound_scope()) == []

    asyncio.run(_run())


@pytest.mark.parametrize("memory_http_url", ["document"], indirect=True)
def test_code_tools_document_roundtrip_and_project_boundary(memory_http_url):
    """Use the same Code tools over HTTP; the server still authorizes each call."""
    async def run():
        provider = AgentMemoryMemoryProvider(
            base_url=memory_http_url, scene="code", project="repo-a",
        )
        await provider.initialize(user_id="code-http", scope_id="tenant-http", session_id="")
        prefix = "agent_memory_coding_memory_"
        try:
            result = json.loads(await provider.handle_tool_call(prefix + "write", {
                "content": "repository uses Python", "project": "forged-other"}))
            assert result["stored"] is True
            read = await provider.handle_tool_call(prefix + "read", {"md_filename": "MEMORY.md"})
            assert "Python" in read
            edited = await provider.handle_tool_call(prefix + "edit", {
                "md_filename": "MEMORY.md", "old_text": "Python", "new_text": "Rust"})
            assert "error" not in json.loads(edited)
            read = await provider.handle_tool_call(prefix + "read", {"md_filename": "MEMORY.md"})
            assert "Rust" in read and "Python" not in read
            hits = await provider.handle_tool_call(prefix + "search", {"query": "Rust"})
            assert "Rust" in hits
        finally:
            await provider.shutdown()
        other = AgentMemoryMemoryProvider(base_url=memory_http_url, scene="code", project="repo-b")
        await other.initialize(user_id="code-http", scope_id="tenant-http", session_id="")
        try:
            hits = json.loads(await other.handle_tool_call(prefix + "search", {"query": "Rust"}))
            assert hits["results"] == []
        finally:
            await other.shutdown()
    asyncio.run(run())



@pytest.mark.parametrize("memory_http_url", ["document"], indirect=True)
def test_http_work_code_profile_scope_sync_and_restart(memory_http_url, monkeypatch):
    """V-03/V-07/V-08: real HTTP documents, with deterministic classification."""
    from jiuwen_memory.construction.extractor_impl.keyword_extractor import KeywordExtractor

    original = KeywordExtractor.extract

    def classify(self, units, **kwargs):
        derived = original(self, units, **kwargs)
        for unit in derived:
            if unit.content.startswith("User prefers"):
                unit.system_metadata["memory_class"] = "user_profile"
        return derived

    monkeypatch.setattr(KeywordExtractor, "extract", classify)

    async def run():
        providers = []

        async def open_provider(scene="code", project="profile-repo-a", agent="profile-agent"):
            provider = AgentMemoryMemoryProvider(
                base_url=memory_http_url, scene=scene, project=project, agent_id=agent)
            await provider.initialize(user_id="profile-user", scope_id="tenant", session_id="")
            providers.append(provider)
            assert type(provider._client).__name__ == "_HttpClient"
            assert provider._config_dict is None and provider._security_provider is None
            return provider

        prefix = "agent_memory_coding_memory_"
        try:
            code = await open_provider()
            await code.handle_tool_call(
                prefix + "write", {"content": "User prefers concise answers"},
            )
            await code.sync_turn("The repository build marker is Cedar7319.", "Understood.")
            work = await open_provider("agent")
            recalled = await work.prefetch("User prefers concise answers Cedar7319")
            assert "concise" in recalled and "Cedar7319" not in recalled
            await work.handle_tool_call("agent_memory_conclude", {
                "conclusion": "User prefers dark theme"})
            for project, expected in [("profile-repo-a", True), ("profile-repo-b", False)]:
                other = await open_provider(project=project)
                recalled = await other.prefetch("Cedar7319 concise dark theme")
                assert "dark theme" in recalled and "concise" in recalled
                assert ("Cedar7319" in recalled) == expected
            isolated = await open_provider(agent="another-profile-agent")
            assert not await isolated.prefetch("Cedar7319 concise dark theme")
            await code.shutdown()
            reopened = await open_provider()
            assert "Cedar7319" in await reopened.prefetch("Cedar7319")
        finally:
            for provider in providers:
                await provider.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("memory_http_url", ["document"], indirect=True)
def test_http_denied_scope_does_not_claim_write_and_recovers(memory_http_url):
    """The real server denies a target outside its authenticated USER boundary."""
    async def run():
        provider = AgentMemoryMemoryProvider(
            base_url=memory_http_url, scene="code", project="denied-repo",
            identity_user="not-the-authenticated-user")
        await provider.initialize(user_id="denied-user", scope_id="tenant", session_id="")
        prefix = "agent_memory_coding_memory_"
        try:
            result = json.loads(await provider.handle_tool_call(prefix + "write", {
                "content": "Denied writes must not become memories."}))
            assert "error" in result and result.get("stored") is not True
            denied = json.loads(await provider.handle_tool_call(prefix + "read", {
                "md_filename": "MEMORY.md"}))
            assert "403" in denied["error"]
            provider._client._identity_user = "developer"
            before = json.loads(await provider.handle_tool_call(prefix + "read", {
                "md_filename": "MEMORY.md"}))
            assert not before.get("blocks")
            accepted = json.loads(await provider.handle_tool_call(prefix + "write", {
                "content": "Authorized repository fact."}))
            assert accepted["stored"] is True
        finally:
            await provider.shutdown()

    asyncio.run(run())


def test_http_unreachable_never_constructs_sdk_or_claims_success(monkeypatch):
    import socket

    import agent_memory_provider as provider_module

    def reject_sdk(*args, **kwargs):
        pytest.fail("HTTP must not construct an in-process runtime")

    monkeypatch.setattr(provider_module._InProcessClient, "__init__", reject_sdk)

    async def run(port):
        provider = AgentMemoryMemoryProvider(base_url=f"http://127.0.0.1:{port}", scene="code")
        try:
            await provider.initialize(user_id="offline", scope_id="tenant", session_id="")
            result = json.loads(await provider.handle_tool_call(
                "agent_memory_coding_memory_write", {"content": "unreachable fact"}))
            assert "error" in result and result.get("stored") is not True
            assert type(provider._client).__name__ == "_HttpClient"
        finally:
            await provider.shutdown()

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        asyncio.run(run(reserved.getsockname()[1]))
