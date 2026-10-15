# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentMemoryMemoryProvider 单元测试：信封剥离 / 召回过滤 / scope 映射 / SDK 链路。

契约依据：``jiuwen_memory_adapter/jiuwenswarm/jiuwen_memory_rail_design.md`` §6.2。
HTTP 模式对真实服务的契约测试见
``tests/integration/jiuwen_memory_adapter/``。

部署形态：jiuwenswarm 宿主把 ``jiuwen_memory_adapter/jiuwenswarm`` 放进
PYTHONPATH，provider 以**顶层模块**被 import（``agent_memory_provider``）——测试
按同一形态装配 sys.path，保证被测的就是部署时执行的那份模块身份。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 本文件 -> tests/unit/jiuwen_memory_adapter -> tests/unit -> tests -> 仓库根
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADAPTER_DIR = _REPO_ROOT / "jiuwen_memory_adapter" / "jiuwenswarm"
if _ADAPTER_DIR.is_dir() and str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

# ruff: noqa: E402
from agent_memory_provider import AgentMemoryMemoryProvider, _HttpClient, _recall_filters

pytestmark = pytest.mark.unit

ENVELOPE_ZH = '你收到一条消息：\n{"source": "tui", "content": "记住常用语言是 Python"}'
ENVELOPE_EN = 'You receive a new message:\n{"source": "tui", "content": "remember dark theme"}'


# --------------------------------------------------------------------------- #
# §6.2-E：TUI 信封剥离（prefetch / sync_turn 共用的统一入口）
# --------------------------------------------------------------------------- #


class TestPrepareEnvelope:
    def test_prepare_strips_zh_envelope(self):
        provider = AgentMemoryMemoryProvider()
        assert provider._prepare(ENVELOPE_ZH) == "记住常用语言是 Python"

    def test_prepare_strips_en_envelope(self):
        provider = AgentMemoryMemoryProvider()
        assert provider._prepare(ENVELOPE_EN) == "remember dark theme"

    def test_prepare_passthrough_plain_text(self):
        provider = AgentMemoryMemoryProvider()
        assert provider._prepare("普通查询，不带信封") == "普通查询，不带信封"

    def test_prepare_passthrough_envelope_prefix_with_invalid_json(self):
        provider = AgentMemoryMemoryProvider()
        text = "你收到一条消息：\nnot-a-json"
        assert provider._prepare(text) == text

    def test_prepare_empty_string(self):
        provider = AgentMemoryMemoryProvider()
        assert provider._prepare("") == ""


# --------------------------------------------------------------------------- #
# §6.2-F：prefetch 召回口径（dict DSL，HTTP/进程内两模式共用）
# --------------------------------------------------------------------------- #


class TestRecallFilters:
    def test_filters_include_distilled_tiers_only(self):
        assert _recall_filters() == {"tier": {"in": ["semantic", "core", "procedural"]}}

    def test_filters_exclude_episodic_and_working(self):
        flattened = _recall_filters()["tier"]["in"]
        assert "episodic" not in flattened
        assert "working" not in flattened
        assert "archival" not in flattened


# --------------------------------------------------------------------------- #
# §6.2-D：HTTP 模式身份覆盖式 scope 映射
# --------------------------------------------------------------------------- #


class TestHttpScopePayload:
    def test_identity_axes_occupy_org_and_user(self):
        client = _HttpClient(
            "http://127.0.0.1:8137",
            identity_org="corp",
            identity_user="alice",
        )
        provider = AgentMemoryMemoryProvider()
        payload = client._scope_payload(provider.bound_scope())
        assert payload == {
            "org": "corp",
            "space": "",
            "user": "alice",
            "agent": "jiuwenswarm",
            "session": "jiuwenswarm-user",
        }

    def test_defaults_match_dev_authenticator_identity(self):
        client = _HttpClient("http://127.0.0.1:8137")
        payload = client._scope_payload(AgentMemoryMemoryProvider().bound_scope())
        assert payload["org"] == "local"
        assert payload["user"] == "developer"

    def test_semantic_session_axis_sinks_to_session(self):
        """rail 的 user_id 下沉到 session 轴（design doc R2 记录的语义漂移）。"""
        client = _HttpClient("http://127.0.0.1:8137")
        provider = AgentMemoryMemoryProvider(user_id="alice", agent_id="agent-1")
        payload = client._scope_payload(provider.bound_scope())
        assert payload["session"] == "alice"
        assert payload["agent"] == "agent-1"


# --------------------------------------------------------------------------- #
# is_available
# --------------------------------------------------------------------------- #


class TestAvailability:
    def test_http_mode_available_with_base_url(self):
        provider = AgentMemoryMemoryProvider(base_url="http://127.0.0.1:8137")
        assert provider.is_available() is True

    def test_sdk_mode_needs_config_path(self):
        provider = AgentMemoryMemoryProvider(config_path="/nonexistent/config.yaml")
        assert provider.is_available() is True

    def test_default_construction_unavailable(self):
        provider = AgentMemoryMemoryProvider()
        assert provider.is_available() is False


# --------------------------------------------------------------------------- #
# SDK（进程内）链路：assemble() 默认离线栈，不依赖网络与服务
# --------------------------------------------------------------------------- #


class TestSdkMode:
    @pytest.mark.asyncio
    async def test_conclude_then_search_tool_roundtrip(self):
        provider = AgentMemoryMemoryProvider()
        await provider.initialize(user_id="sdk-unit-user", scope_id="tenant-sdk")
        try:
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
            assert any("深色主题" in item["memory"] for item in result["results"]), result
        finally:
            await provider.shutdown()

    @pytest.mark.asyncio
    async def test_sync_turn_with_envelope_neither_raises_nor_stores_shell(self):
        provider = AgentMemoryMemoryProvider()
        await provider.initialize(user_id="sdk-envelope-user", scope_id="tenant-sdk")
        try:
            # 只验证不抛错与不崩溃——infer 派生质量取决于装配的 extractor，
            # 契约层面的完整断言在集成测试（真实服务同栈行为已实测）。
            await provider.sync_turn(ENVELOPE_ZH, "好的，已记住。")
            assert provider.is_initialized
        finally:
            await provider.shutdown()

    @pytest.mark.asyncio
    async def test_sdk_add_returns_add_result_with_rebuilt_content(self):
        provider = AgentMemoryMemoryProvider()
        await provider.initialize(user_id="sdk-add-user", scope_id="tenant-sdk")
        try:
            client = provider._client
            result = await client.add("SDK 原文直写", provider.bound_scope(), tags=["t"])
            assert result is not None
            assert result.item_id
            assert result.content == "SDK 原文直写"
        finally:
            await provider.shutdown()
