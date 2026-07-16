# coding: utf-8
"""mcp_server UT —— 长期记忆 MCP 工具层（桩引擎隔离 LLM/存储）。

被测对象：mcp_server 对外公开的 7 个工具函数 + health_check + main 传输选择。
工具层契约：参数透传、返回值序列化、异常兜底、delete_all confirm 守卫。

引擎全桩（AsyncMock），不触达真实 LongTermMemory 装配。
不依赖 pytest-mock：统一用标准库 unittest.mock + monkeypatch，保证零额外插件即可跑通。
测试仅引用 mcp_server 的公开成员，不导入/不直接测试任何私有成员或私有方法。
"""
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwen_memory.server import mcp_server
from jiuwen_memory.server.mcp_server import (
    DEFAULT_SCOPE_ID,
    DEFAULT_USER_ID,
    main,
    reset_engine,
)


# --------------------------------------------------------------------------- #
# 桩引擎：返回全 AsyncMock 的引擎替身，工具层只 await 其方法并读 is_ready。
# 引擎方法返回值须为可直接 JSON 化的 dict/list（工具层 _json 直接序列化）。
# --------------------------------------------------------------------------- #
def _stub_engine(ready: bool = True) -> MagicMock:
    engine = MagicMock()
    engine.is_ready = ready
    for m in (
        "add_messages", "search_memories", "search_history_summaries",
        "get_memories", "update_memory", "delete_memory", "delete_all_memories",
    ):
        setattr(engine, m, AsyncMock())
    return engine


@pytest.fixture
def stub_engine(monkeypatch):
    """注入桩引擎，返回供用例配置 return_value/side_effect。"""
    engine = _stub_engine(ready=True)

    async def _fake_get_engine():
        return engine

    monkeypatch.setattr(mcp_server, "_get_engine", _fake_get_engine, raising=True)
    return engine


@pytest.fixture
def failing_engine(monkeypatch):
    """引擎取用直接抛异常，用于 health_check 的 unavailable 分支。"""

    async def _boom():
        raise RuntimeError("init failed")

    monkeypatch.setattr(mcp_server, "_get_engine", _boom, raising=True)


# --------------------------------------------------------------------------- #
# 工具层 —— add_messages
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_add_messages_passthrough(stub_engine):
    stub_engine.add_messages.return_value = {
        "status": "added", "infer": False, "user_profile": "p",
        "semantic_memory": "s", "episodic_memory": "e",
        "summary": "sm", "variables": "v",
    }
    out = json.loads(await mcp_server.add_messages(
        messages=[{"role": "user", "content": "hi"}],
        user_id="u", scope_id="s", infer=False,
    ))
    call = stub_engine.add_messages.call_args
    assert call.kwargs["user_id"] == "u"
    assert call.kwargs["scope_id"] == "s"
    assert call.kwargs["infer"] is False
    assert out["status"] == "added"
    assert out["infer"] is False
    assert out["user_profile"] == "p"
    assert out["semantic_memory"] == "s"


@pytest.mark.asyncio
async def test_add_messages_error(stub_engine):
    stub_engine.add_messages.side_effect = RuntimeError("boom")
    out = json.loads(await mcp_server.add_messages(
        messages=[{"role": "user", "content": "x"}],
    ))
    assert "error" in out and "add_messages" in out["error"]


@pytest.mark.asyncio
async def test_add_messages_default_ids(stub_engine):
    stub_engine.add_messages.return_value = {
        "status": "added", "infer": True, "user_profile": None,
        "semantic_memory": None, "episodic_memory": None,
        "summary": None, "variables": None,
    }
    await mcp_server.add_messages(messages=[{"role": "user", "content": "x"}])
    call = stub_engine.add_messages.call_args
    assert call.kwargs["user_id"] == DEFAULT_USER_ID
    assert call.kwargs["scope_id"] == DEFAULT_SCOPE_ID


# --------------------------------------------------------------------------- #
# 工具层 —— search_memories
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_search_memories_passthrough(stub_engine):
    stub_engine.search_memories.return_value = [
        {"mem_id": "m1", "content": "c1", "type": "semantic_memory", "score": 0.9},
    ]
    out = json.loads(await mcp_server.search_memories(
        query="q", num=5, user_id="u", scope_id="s", threshold=0.3,
    ))
    call = stub_engine.search_memories.call_args
    assert call.kwargs == {
        "query": "q", "num": 5, "user_id": "u", "scope_id": "s", "threshold": 0.3,
    }
    assert out["count"] == 1
    assert out["results"][0]["mem_id"] == "m1"


@pytest.mark.asyncio
async def test_search_memories_error(stub_engine):
    stub_engine.search_memories.side_effect = ValueError("bad query")
    out = json.loads(await mcp_server.search_memories(query="q"))
    assert "error" in out and "search_memories" in out["error"]


# --------------------------------------------------------------------------- #
# 工具层 —— search_history_summaries
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_search_history_summaries_passthrough(stub_engine):
    stub_engine.search_history_summaries.return_value = [
        {"mem_id": "h1", "content": "conv1", "type": "summary", "score": 0.8},
    ]
    out = json.loads(await mcp_server.search_history_summaries(
        query="q", num=3, user_id="u", scope_id="s", threshold=0.3,
    ))
    call = stub_engine.search_history_summaries.call_args
    assert call.kwargs["num"] == 3
    assert out["count"] == 1
    assert out["results"][0]["content"] == "conv1"


# --------------------------------------------------------------------------- #
# 工具层 —— get_memories
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_memories_serialization(stub_engine):
    stub_engine.get_memories.return_value = [
        {"mem_id": "m1", "content": "c1", "type": "semantic_memory",
         "timestamp": "2026-07-16T10:00:00"},
    ]
    out = json.loads(await mcp_server.get_memories(
        page_size=10, page_idx=2, memory_type="unknown",
    ))
    assert out["page_idx"] == 2
    assert out["count"] == 1
    assert out["results"][0]["timestamp"] == "2026-07-16T10:00:00"
    assert out["results"][0]["mem_id"] == "m1"


# --------------------------------------------------------------------------- #
# 工具层 —— update_memory
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_update_memory_passthrough(stub_engine):
    stub_engine.update_memory.return_value = {"status": "updated", "mem_id": "m1"}
    out = json.loads(await mcp_server.update_memory(
        mem_id="m1", memory="new", user_id="u", scope_id="s",
    ))
    call = stub_engine.update_memory.call_args
    assert call.kwargs["mem_id"] == "m1"
    assert call.kwargs["memory"] == "new"
    assert call.kwargs["user_id"] == "u"
    assert out["status"] == "updated"


@pytest.mark.asyncio
async def test_update_memory_error(stub_engine):
    stub_engine.update_memory.side_effect = RuntimeError("nope")
    out = json.loads(await mcp_server.update_memory(mem_id="m1", memory="x"))
    assert "error" in out and "update_memory" in out["error"]


# --------------------------------------------------------------------------- #
# 工具层 —— delete_memory
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delete_memory_passthrough(stub_engine):
    stub_engine.delete_memory.return_value = {"status": "deleted", "mem_id": "m1"}
    out = json.loads(await mcp_server.delete_memory(
        mem_id="m1", user_id="u", scope_id="s",
    ))
    call = stub_engine.delete_memory.call_args
    assert call.kwargs["mem_id"] == "m1"
    assert out["status"] == "deleted"


@pytest.mark.asyncio
async def test_delete_memory_error(stub_engine):
    stub_engine.delete_memory.side_effect = RuntimeError("denied")
    out = json.loads(await mcp_server.delete_memory(mem_id="m1"))
    assert "error" in out and "delete_memory" in out["error"]


# --------------------------------------------------------------------------- #
# 工具层 —— delete_all_memories（confirm 守卫）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delete_all_aborted_without_confirm(stub_engine):
    out = json.loads(await mcp_server.delete_all_memories(confirm=False))
    assert out["status"] == "aborted"
    assert "irreversible" in out["reason"].lower()
    stub_engine.delete_all_memories.assert_not_called()


@pytest.mark.asyncio
async def test_delete_all_confirmed_passthrough(stub_engine):
    stub_engine.delete_all_memories.return_value = {"status": "deleted", "scope_id": "s1"}
    out = json.loads(await mcp_server.delete_all_memories(scope_id="s1", confirm=True))
    call = stub_engine.delete_all_memories.call_args
    assert call.kwargs["scope_id"] == "s1"
    assert out["status"] == "deleted"


@pytest.mark.asyncio
async def test_delete_all_error(stub_engine):
    stub_engine.delete_all_memories.side_effect = RuntimeError("wipe failed")
    out = json.loads(await mcp_server.delete_all_memories(confirm=True))
    assert "error" in out and "delete_all_memories" in out["error"]


# --------------------------------------------------------------------------- #
# 工具层 —— health_check
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_health_check_ready(stub_engine):
    stub_engine.is_ready = True
    out = json.loads(await mcp_server.health_check())
    assert out["status"] == "healthy"
    assert out["ready"] is True


@pytest.mark.asyncio
async def test_health_check_unavailable(failing_engine):
    out = json.loads(await mcp_server.health_check())
    assert out["status"] == "unavailable"
    assert "init failed" in out["error"]


# --------------------------------------------------------------------------- #
# 公开 API —— reset_engine
# --------------------------------------------------------------------------- #
def test_reset_engine_is_callable():
    """reset_engine 是公开 API，调用不报错即可（重建语义由后续调用体现）。"""
    reset_engine()
    reset_engine()


# --------------------------------------------------------------------------- #
# main 传输选择
# --------------------------------------------------------------------------- #
def _inject_fake_uvicorn(monkeypatch):
    fake_mod = types.ModuleType("uvicorn")
    fake_run = MagicMock()
    fake_mod.run = fake_run
    monkeypatch.setitem(sys.modules, "uvicorn", fake_mod)
    return fake_run


def test_main_http_invokes_uvicorn(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_PORT", "8765")
    fake_run = _inject_fake_uvicorn(monkeypatch)
    main()
    fake_run.assert_called_once()
    _, kwargs = fake_run.call_args
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8765


def test_main_sse_uses_sse_app(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "sse")
    fake_run = _inject_fake_uvicorn(monkeypatch)
    with patch.object(mcp_server.mcp, "sse_app", return_value=MagicMock()) as sse_app:
        main()
    fake_run.assert_called_once()
    sse_app.assert_called_once()


def test_main_invalid_transport(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "weird")
    with pytest.raises(ValueError, match="Unsupported MCP_TRANSPORT"):
        main()
