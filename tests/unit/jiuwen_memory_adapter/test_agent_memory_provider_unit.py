# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentMemoryMemoryProvider 单元测试：信封剥离 / 召回过滤 / scope 映射 / SDK 链路。

契约依据：``jiuwen_memory_adapter/jiuwenswarm/jiuwen_memory_rail_design.md`` §6.2。
HTTP 模式对真实服务的契约测试见
``tests/integration/jiuwen_memory_adapter/``。

部署形态：jiuwenswarm 宿主把 ``jiuwen_memory_adapter/jiuwenswarm`` 放进
PYTHONPATH，provider 以**顶层模块**被 import（``agent_memory_provider``）——测试
按同一形态装配 sys.path，保证被测的就是部署时执行的那份模块身份。
"""

# Pytest 类只用于分组，测试方法按 pytest 约定保留实例形态。
# pylint: disable=add-staticmethod-or-classmethod-decorator
# 测试直取 provider/client 受保护成员（_prepare / _scope_payload / _client）以断言内部接线行为。
# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
# 前置 sys.path 对齐部署形态的顶层模块 import（详见下方 _ADAPTER_DIR 说明）。

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from jiuwen_memory.api import (
    ConflictError,
    Modality,
    Surface,
    ValidationError,
    build_dev_authenticator,
    internal_context,
)
from jiuwen_memory.api import Scope as ApiScope
from jiuwen_memory.api import normalize as normalize_filters
from jiuwen_memory.common.normalizer.normalizer_impl.video_normalizer import VideoNormalizer
from jiuwen_memory.common.security import new_request_context
from jiuwen_memory.common.security.types import AuthContext, Role
from jiuwen_memory.common.type_def.filter import FilterLogic
from jiuwen_memory.construction.extractor_impl.agent_llm_extractor import AgentLlmExtractor
from tests.support.whitebox import private, private_method

# 本文件 -> tests/unit/jiuwen_memory_adapter -> tests/unit -> tests -> 仓库根
# append（而非 insert(0)）：避免模块重名告警；adapter 作为顶层模块 agent_memory_provider
# 被加载，仓库内无同名包冲突，尾部追加即命中（参照 test_jiuwenswarm_access.py）。
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADAPTER_DIR = _REPO_ROOT / "jiuwen_memory_adapter" / "jiuwenswarm"
if _ADAPTER_DIR.is_dir() and str(_ADAPTER_DIR) not in sys.path:
    sys.path.append(str(_ADAPTER_DIR))

# ruff: noqa: E402
# pylint: disable=wrong-import-order  # 顶层模块须晚于 sys.path.append，无法按 first/third-party 排序
from agent_memory_provider import (
    AgentMemoryClientError,
    AgentMemoryMemoryProvider,
    _HttpClient,
    _InProcessClient,
    _merge_prefetch_lines,
    _recall_filters,
    _tail_within_budget,
)

pytestmark = pytest.mark.unit


class _DelegatingClient:
    """Forward client operations without inheriting its concrete type."""

    def __init__(self, client: _InProcessClient) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _EnglishLikeValue:
    """Compare equal to the English language code."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and other == "en"


@pytest.mark.parametrize("language", ["fr", False, ["en"], _EnglishLikeValue()])
def test_provider_rejects_unsupported_portal_language(language):
    """Check type validation before matching supported language codes."""
    with pytest.raises(ValueError, match="Portal language"):
        AgentMemoryMemoryProvider(portal={"language": language})


@pytest.mark.parametrize("extension", ["json", "yaml"])
def test_sdk_config_path_preserves_literal_prompt(tmp_path, monkeypatch, extension):
    monkeypatch.delenv("PORTAL_TEST_PLACEHOLDER", raising=False)
    prompt = "literal ${PORTAL_TEST_PLACEHOLDER}"
    path = tmp_path / f"config.{extension}"
    # JSON syntax is accepted by YAML as well.
    path.write_text(json.dumps({"prompts": {"extract": {"fact": prompt}}}), encoding="utf-8")
    client = _InProcessClient(
        str(path),
        security_provider=_security_provider(ApiScope(org="local", user="developer")),
    )
    try:
        assert client.runtime._kernel.config_source.fetch("prompts.extract.fact") == prompt
    finally:
        asyncio.run(client.close())


def test_sdk_kv_configuration_uses_only_the_loaded_config_file(tmp_path):
    config = {"llm": {"default": {"target": "echo", "params": {"llm_model": "before"}}}}
    path = tmp_path / "memory.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    auth = build_dev_authenticator()

    def security():
        return internal_context(auth)

    client = _InProcessClient(str(path), security_provider=security)
    try:
        assert client.portal_location is None
        result = client.runtime.configuration.save(
            0, {"llm.default.params.llm_model": "after"}, security=security(),
        )
        assert result == {"version": 1, "applied": True}
    finally:
        asyncio.run(client.close())

    restarted = _InProcessClient(str(path), security_provider=security)
    try:
        assert restarted.runtime._kernel.config_source.fetch("llm.model") == "after"
    finally:
        asyncio.run(restarted.close())

    from_dict = _InProcessClient(str(path), config_dict=config, security_provider=security)
    try:
        assert from_dict.runtime.configuration is None
        assert from_dict.runtime._kernel.config_source.fetch("llm.model") == "before"
    finally:
        asyncio.run(from_dict.close())


def _security_provider(actor: ApiScope, *, role: Role = Role.USER):
    """测试用可信 security provider：模拟已完成认证的 composition root。

    mem2.0 起进程内模式不从业务 scope 自述身份——actor 只能由注入的可信 provider
    产出（生产等价物是 API Key / Trusted Gateway 的认证结论）。返回的 callable
    每次调用给出同一份已认证上下文，与业务 target 无关。
    """
    context = new_request_context(
        AuthContext(
            actor=actor,
            role=role,
            credential_type="internal",
            auth_method="internal",
            authenticated_at=datetime.now(UTC),
        ),
        surface=Surface.INTERNAL,
    )
    return lambda: context


@pytest.fixture(autouse=True)
def _offline_in_process_config(tmp_path, monkeypatch):
    """TestSdkMode 走进程内默认装配 ``_agent_default_config()``——生产字面量连真实
    deepseek LLM / siliconflow embedder / 远端 ES，且 md root 写真实磁盘
    ``e:/02_memory/agent-memory/_md_store``。测试环境无 LLM 凭证会令 search 的
    query_parser 抛 OpenAIError，且会污染生产 md 目录。换全离线栈跑通：

    - ``llm`` → echo（回显桩，无网络；query 改写=原文）
    - ``extractor`` → keyword（离线抽取：chunk 提升 SEMANTIC，不调 LLM——conclude
      现走 infer=true 同步抽取，echo LLM 回显非 JSON 会被 agent_llm extractor
      re-raise，故把 extractor 一并换成无 LLM 依赖的 keyword）
    - ``embedder`` → hashing（确定性 hash 词袋向量，dim=64，与 globals 对齐）
    - ``fulltext_store`` → memory（不连远端 ES；document 模式召回走 shadow FTS5）
    - ``markdown_store`` root → tmp_path（隔离写盘，不污染生产 md 目录）
    - ``watch_document`` → False（不起文件监听线程，避免测试后残留）

    仅影响实际 ``initialize`` SDK client 的 TestSdkMode；TestPrepareEnvelope /
    TestRecallFilters / TestHttpScopePayload / TestAvailability 不装配 client，
    不受影响。
    """
    import agent_memory_provider as _amp

    for name in (
        "JIUWEN_MEMORY_VIDEO_ROOTS", "JIUWEN_MEMORY_VIDEO_JOB_DB", "ASR_API_BASE",
        "ASR_MODEL_NAME", "ASR_API_KEY", "VIDEO_API_BASE", "VIDEO_MODEL_NAME", "VIDEO_API_KEY",
        "JIUWEN_MEMORY_EXPERIENCE_AUTHOR_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "swarm"))
    _orig = _amp._agent_default_config

    def _offline(*, experience_space: str = "") -> dict:
        cfg = _orig(experience_space=experience_space)
        cfg["globals"]["embedder_dim"] = 64
        cfg["globals"]["watch_document"] = False
        cfg["llm"]["default"] = "echo"
        cfg["extractor"].update(default="keyword", document_fact="keyword")
        cfg["tokenizer"] = {"default": {"target": "whitespace"}}
        cfg["shadow_index"]["default"]["params"]["tokenizer"] = "default"
        cfg["embedder"] = {"default": {"target": "hashing", "params": {"tokenizer": "default"}}}
        cfg["fulltext_store"] = {"default": {"target": "memory"}}
        cfg["markdown_store"] = {"default": {"target": "local", "params": {"root": str(tmp_path)}}}
        return cfg

    monkeypatch.setattr(_amp, "_agent_default_config", _offline)
    yield


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
        assert _recall_filters() == {
            "tier": {"in": ["semantic", "core", "procedural"]},
            "system_metadata.memory_class": {"ne": "team_memory"},
        }

    def test_filters_exclude_episodic_and_working(self):
        flattened = _recall_filters()["tier"]["in"]
        assert "episodic" not in flattened
        assert "working" not in flattened
        assert "archival" not in flattened

    def test_filters_with_project_add_equal_predicate(self):
        filters = _recall_filters("proj-a")
        assert filters["system_metadata.project"] == {"eq": "proj-a"}, filters
        assert filters["tier"] == {"in": ["semantic", "core", "procedural"]}, filters

    def test_filters_without_project_carry_no_project_predicate(self):
        assert "system_metadata.project" not in _recall_filters(""), "空 project 不应带项目谓词"

    def test_filters_with_project_normalize_to_and_clauses(self):
        # 层级、项目和排除团队类别由 API 边界统一解析为 AND。
        expr = normalize_filters(_recall_filters("proj-a"))
        assert expr.logic is FilterLogic.AND, expr
        clauses = {(c.field, c.op.value): c.value for c in expr.children}
        assert clauses == {
            ("tier", "in"): ["semantic", "core", "procedural"],
            ("system_metadata.project", "eq"): "proj-a",
            ("system_metadata.memory_class", "ne"): "team_memory",
        }, clauses


# --------------------------------------------------------------------------- #
# prefetch 直接注入 USER.md
# --------------------------------------------------------------------------- #


class TestProfileInjection:
    def test_tail_within_budget_keeps_latest_items_in_order(self):
        items = [{"content": f"事实{i}"} for i in range(5)]

        assert [it["content"] for it in _tail_within_budget(items, max_items=2, max_chars=100)] == [
            "事实3",
            "事实4",
        ]
        assert [it["content"] for it in _tail_within_budget(items, max_items=10, max_chars=7)] == [
            "事实3",
            "事实4",
        ], "字数预算同样从末尾起算"
        assert _tail_within_budget([], max_items=2, max_chars=10) == []

    def test_tail_within_budget_skips_oversized_item(self):
        items = [{"content": "旧事实"}, {"content": "超长" * 10}]

        kept = _tail_within_budget(items, max_items=5, max_chars=10)

        assert [it["content"] for it in kept] == ["旧事实"], "最新一条超长时跳过它，仍注入其余条目"

    def test_merge_puts_profile_first_and_dedups_by_id_and_text(self):
        profile = [
            {"item_id": "p1", "content": "发件邮箱是 a@qq.com"},
            {"item_id": "p2", "content": "偏好简洁回答"},
        ]
        hits = [
            {"item_id": "p2", "content": "偏好简洁回答"},
            {"item_id": "h1", "content": "发件邮箱是 a@qq.com"},
            {"item_id": "h2", "content": "项目部署在杭州"},
        ]

        assert _merge_prefetch_lines(profile, hits) == [
            "发件邮箱是 a@qq.com",
            "偏好简洁回答",
            "项目部署在杭州",
        ]

    def test_prefetch_injects_profile_regardless_of_query_wording(self):
        async def _run() -> None:
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-profile-user"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-profile-user", scope_id="tenant-sdk")
            try:
                seen: list[tuple] = []

                async def _read(md_filename, scope, *, project="", line_range=None):
                    seen.append((md_filename, project))
                    fact = "用户的发件邮箱是 a@qq.com"
                    return [{"item_id": "p1", "content": fact, "tier": "semantic"}]

                provider._client.read_md_file = _read
                # 与画像条目没有任何共同词的问法：检索侧取不到，画像仍被注入
                text = await provider.prefetch("把这个 PDF 发一下")
                assert "用户的发件邮箱是 a@qq.com" in text, text
                assert seen == [("USER.md", "")], seen
            finally:
                await provider.shutdown()

        asyncio.run(_run())

    def test_prefetch_survives_profile_read_failure(self):
        async def _run() -> None:
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-profile-fail"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-profile-fail", scope_id="tenant-sdk")
            try:
                async def _boom(md_filename, scope, *, project="", line_range=None):
                    raise RuntimeError("read failed")

                provider._client.read_md_file = _boom
                text = await provider.prefetch("把这个 PDF 发一下")
                assert text == "", "读画像失败时降级为只用检索结果"
            finally:
                await provider.shutdown()

        asyncio.run(_run())

    def test_prefetch_tolerates_profile_read_returning_none(self):
        async def _run() -> None:
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-profile-none"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-profile-none", scope_id="tenant-sdk")
            try:
                async def _none(md_filename, scope, *, project="", line_range=None):
                    return None

                provider._client.read_md_file = _none
                assert await provider.prefetch("把这个 PDF 发一下") == "", (
                    "读取返回 None 时按无画像处理"
                )
            finally:
                await provider.shutdown()

        asyncio.run(_run())

    def test_prefetch_keeps_profile_when_search_fails(self):
        async def _run() -> None:
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-search-fail"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-search-fail", scope_id="tenant-sdk")
            try:
                async def _read(md_filename, scope, *, project="", line_range=None):
                    return [{"item_id": "p1", "content": "用户的发件邮箱是 a@qq.com"}]

                async def _boom(*args, **kwargs):
                    raise RuntimeError("search failed")

                provider._client.read_md_file = _read
                provider._client.search = _boom
                text = await provider.prefetch("把这个 PDF 发一下")
                assert "用户的发件邮箱是 a@qq.com" in text, "检索失败时仍注入用户画像"
            finally:
                await provider.shutdown()

        asyncio.run(_run())


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

    def test_sdk_mode_needs_config_path_and_security_provider(self):
        provider = AgentMemoryMemoryProvider(
            config_path="/nonexistent/config.yaml",
            security_provider=_security_provider(ApiScope(org="acme", user="alice")),
        )
        assert provider.is_available() is True

    def test_default_construction_available_with_security_provider(self):
        # [本地修改 2026-10-17] 进程内模式恒可用：默认构造走内置 agent 场景默认装配
        # （_agent_default_config）。mem2.0 起还需注入可信 security_provider——
        # 缺少它 is_available 如实报不可用（fail-closed，见下一测试）。
        provider = AgentMemoryMemoryProvider(
            security_provider=_security_provider(ApiScope(org="acme", user="alice")),
        )
        assert provider.is_available() is True

    def test_in_process_unavailable_without_security_provider(self):
        # fail-closed：无可信 security provider 的进程内模式不可用（P1-2），
        # 不得从业务 user_id/scope_id 自述身份降级为可用。
        provider = AgentMemoryMemoryProvider()
        assert provider.is_available() is False


# --------------------------------------------------------------------------- #
# SDK（进程内）链路：assemble() 默认离线栈，不依赖网络与服务
# --------------------------------------------------------------------------- #


async def _assert_text_only_memory_works(provider: AgentMemoryMemoryProvider) -> None:
    assert {schema["name"] for schema in provider.get_tool_schemas()} == {
        "agent_memory_search", "agent_memory_conclude", "agent_memory_read_md_file",
        "agent_memory_edit_md_file",
    }
    await provider.initialize(user_id="user", scope_id="tenant")
    try:
        result = json.loads(await provider.handle_tool_call(
            "agent_memory_conclude", {"conclusion": "Still stores text."},
        ))
        assert result["item_id"]
    finally:
        await provider.shutdown()


@pytest.mark.parametrize("capabilities, base_url", [
    ({}, ""),
    ({"supports_video_ingest": True}, ""),
    ({"supports_video_ingest": lambda: False}, ""),
    ({"supports_video_ingest": lambda: True}, "http://127.0.0.1:8137"),
])
def test_video_tools_require_sdk_callable_capability(monkeypatch, capabilities, base_url):
    provider = AgentMemoryMemoryProvider(
        base_url=base_url,
        security_provider=_security_provider(ApiScope(org="tenant", user="user")),
    )
    monkeypatch.setattr(provider, "_client", SimpleNamespace(**capabilities))
    assert {schema["name"] for schema in provider.get_tool_schemas()} == {
        "agent_memory_search", "agent_memory_conclude", "agent_memory_read_md_file",
        "agent_memory_edit_md_file",
    }


class TestSdkMode:
    @pytest.mark.parametrize("settings, warning", [
        ({"local_media_roots": [], "asr_model": None, "video_model": " "}, None),
        ({"asr_model": "asr"}, "local_media_roots"),
        ({"video_model": "vision"}, "local_media_roots"),
        ({"local_media_roots": "bad-json"}, "local_media_roots"),
        ({"local_media_roots": [{"scope": {"org": "tenant"}, "root": "/tmp"}]}, "asr_base_url"),
    ])
    def test_incomplete_compact_video_keeps_text_memory(self, settings, warning, caplog):
        provider = AgentMemoryMemoryProvider(
            config_dict={"globals": settings},
            security_provider=_security_provider(ApiScope(org="tenant", user="user")),
        )

        asyncio.run(_assert_text_only_memory_works(provider))
        if warning:
            assert "Built-in SDK video profile skipped" in caplog.text
            assert warning in caplog.text
        else:
            assert "Built-in SDK video profile skipped" not in caplog.text

    @pytest.mark.parametrize("root, scope, reason", [
        ("missing", {"org": "tenant"}, "existing non-volume directory"),
        ("relative", {"org": "tenant"}, "absolute local directory"),
        ("present", {"user": "user"}, "scope requires a nonempty org"),
        ("present", {"org": "tenant", "project": "p"}, "scope requires a nonempty org"),
    ])
    def test_invalid_video_import_bindings_keep_text_memory(
        self, tmp_path, root, scope, reason, caplog,
    ):
        paths = {"missing": str(tmp_path / "missing"), "relative": "imports"}
        provider = AgentMemoryMemoryProvider(
            config_dict={"globals": {
                "local_media_roots": [{"scope": scope, "root": paths.get(root, str(tmp_path))}],
                "asr_base_url": "https://asr.example.invalid/v1", "asr_model": "asr",
                "video_base_url": "https://vision.example.invalid/v1", "video_model": "vision",
            }},
            security_provider=_security_provider(ApiScope(org="tenant", user="user")),
        )

        asyncio.run(_assert_text_only_memory_works(provider))
        assert "Built-in SDK video profile skipped" in caplog.text
        assert reason in caplog.text

    @pytest.mark.parametrize("extension", ["json", "yaml"])
    def test_partial_video_file_keeps_document_memory(self, tmp_path, extension):
        md_root = tmp_path / "document-memory"
        config = {
            "globals": {"video_model": "vision-model"},
            "markdown_store": {"default": {"params": {"root": str(md_root)}}},
        }
        path = tmp_path / f"partial-video.{extension}"
        serialize = json.dumps if extension == "json" else yaml.safe_dump
        path.write_text(serialize(config), encoding="utf-8")
        provider = AgentMemoryMemoryProvider(
            config_path=str(path),
            security_provider=_security_provider(ApiScope(org="tenant", user="user")),
        )

        asyncio.run(_assert_text_only_memory_works(provider))
        document = md_root / "memory" / "MEMORY.md"
        assert "Still stores text." in document.read_text(encoding="utf-8")

    @pytest.mark.parametrize("contents, error", [
        ([], "SDK config must contain a mapping"),
        ({"globals": "video_model"}, "SDK config globals must contain a mapping"),
    ])
    def test_sdk_file_requires_mapping(self, tmp_path, contents, error):
        path = tmp_path / "bad-config.json"
        path.write_text(json.dumps(contents), encoding="utf-8")
        provider = AgentMemoryMemoryProvider(
            config_path=str(path),
            security_provider=_security_provider(ApiScope(org="tenant", user="user")),
        )
        with pytest.raises(ValidationError, match=error):
            asyncio.run(provider.initialize())

    def test_failed_video_discovery_preserves_standard_tools(self, tmp_path):
        provider = AgentMemoryMemoryProvider(
            config_path=str(tmp_path / "missing.yml"),
            security_provider=_security_provider(ApiScope(org="tenant", user="user")),
        )
        assert len(provider.get_tool_schemas()) == 4
        assert "agent_memory_video_submit" not in provider.system_prompt_block()
        with pytest.raises(FileNotFoundError):
            asyncio.run(provider.initialize())

    @pytest.mark.parametrize(
        "configuration",
        ["default", "file-json", "file-yaml", "inline", "globals", "no-pipeline", "wrong-evolver",
         "compact", "compact-json", "compact-yaml", "wrapped", "wrapped-no-pipeline"],
    )
    def test_video_tool_writes_document_units(self, tmp_path, monkeypatch, configuration):
        media = tmp_path / "imports"
        media.mkdir()
        video = media / "corgi.mp4"
        video.write_bytes(b"fixture")
        uri = video.as_uri()
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"fixture")
        other_format = media / "notes.txt"
        other_format.write_text("fixture", encoding="utf-8")

        def normalize(_self, payload):
            assert payload.uri == uri
            assert payload.data == b""
            assert payload.modality == Modality.VIDEO
            assert payload.id == payload.system_metadata["payload_id"]
            return json.dumps({
                "payload_id": payload.id,
                "clips": [{"id": "clip-1", "visual_summary": "A corgi chases a ball",
                           "start_seconds": 0, "end_seconds": 3}],
                "events": [{"id": "event-1", "summary": "The corgi plays fetch",
                            "clip_ids": ["clip-1"], "start_seconds": 0, "end_seconds": 3}],
            })

        monkeypatch.setattr(VideoNormalizer, "normalize", normalize)
        config = {
            "normalizer": {"default": {"target": "routing", "params": {
                "routes": {"video": {"target": "video", "params": {
                    "asr_port": "video", "llm_port": "default", "vlm_port": "default",
                    "local_media_roots": [{
                        "scope": {"org": "tenant", "user": "user", "agent": "jiuwenswarm"},
                        "root": str(media),
                    }],
                }}},
            }}},
            "asr": {"video": {"target": "dashscope_filetrans", "params": {
                "asr_model": "offline",
                "asr_base_url": "https://example.invalid",
                "asr_api_key": "test",
            }}},
            "extractor": {"video": "video_memory"},
            "evolver": {"video": {"target": "orchestrating", "params": {
                "extractor": "video", "index_builder": "default", "layer_annotator": None,
                "dedup_extract": False,
            }}},
            "pipeline": {"default": {"target": "metadata", "params": {
                "route_key": "pipeline", "routes": {"video": "video"},
                "profiles": {
                    "default": {"index_builder": "default", "retriever": "default",
                                "evolver": "default", "classifier": "default"},
                    "video": {
                        "index_builder": "default", "retriever": "default", "evolver": "video",
                    },
                },
            }}},
        }

        config_path = None
        if configuration.startswith("compact"):
            route = config["normalizer"]["default"]["params"]["routes"]["video"]
            config = {"globals": {
                "local_media_roots": route["params"]["local_media_roots"],
                "asr_base_url": "https://example.invalid/v1", "asr_model": "offline-asr",
                "video_base_url": "https://example.invalid/v1", "video_model": "offline-vision",
            }}
            if configuration != "compact":
                extension = configuration.removeprefix("compact-")
                path = tmp_path / f"compact.{extension}"
                serialize = json.dumps if extension == "json" else yaml.safe_dump
                path.write_text(serialize(config), encoding="utf-8")
                config_path, config = str(path), None
        elif configuration in {"default", "file-json", "file-yaml"}:
            route = config["normalizer"]["default"]["params"]["routes"]["video"]
            monkeypatch.setenv("JIUWEN_MEMORY_VIDEO_ROOTS", json.dumps(
                route["params"]["local_media_roots"],
            ))
            for name, value in {
                "ASR_API_BASE": "https://example.invalid/v1",
                "ASR_MODEL_NAME": "offline-asr",
                "VIDEO_API_BASE": "https://example.invalid/v1",
                "VIDEO_MODEL_NAME": "offline-vision",
            }.items():
                monkeypatch.setenv(name, value)
            if configuration.startswith("file-"):
                extension = configuration.removeprefix("file-")
                path = tmp_path / f"agent.{extension}"
                file_config = {"markdown_store": {"default": {"params": {
                    "root": str(tmp_path / "file-memory"),
                }}}}
                serialize = json.dumps if extension == "json" else yaml.safe_dump
                path.write_text(serialize(file_config), encoding="utf-8")
                config_path = str(path)
            config = None
        elif configuration == "globals":
            route = config["normalizer"]["default"]["params"]["routes"]["video"]
            config["globals"] = {"local_media_roots": route["params"].pop("local_media_roots")}
        elif configuration in {"no-pipeline", "wrapped-no-pipeline"}:
            del config["pipeline"]
        elif configuration == "wrong-evolver":
            config["pipeline"]["default"]["params"]["profiles"]["video"]["evolver"] = "default"

        async def run():
            holder = None
            if configuration.startswith("wrapped"):
                holder = SimpleNamespace(
                    acquire=lambda _config, _security, factory: _DelegatingClient(factory()),
                )
            provider = AgentMemoryMemoryProvider(
                config_dict=config,
                config_path=config_path,
                security_provider=_security_provider(ApiScope(org="tenant", user="user")),
                document_runtime_holder=holder,
            )
            enabled = configuration not in {"no-pipeline", "wrong-evolver", "wrapped-no-pipeline"}
            schemas = {s["name"]: s for s in provider.get_tool_schemas()}
            runtime = provider.runtime
            assert runtime is not None
            assert ("agent_memory_video_submit" in schemas) is enabled
            assert ("agent_memory_video_status" in schemas) is enabled
            early = await provider.handle_tool_call("agent_memory_video_submit", {"uri": uri})
            assert json.loads(early) == {"error": "provider not initialized"}
            await provider.initialize(user_id="user", scope_id="tenant", session_id="")
            try:
                assert provider.runtime is runtime
                if config_path:
                    assert runtime.configuration is not None
                    document_root = (
                        tmp_path / "file-memory" if configuration.startswith("file-") else tmp_path
                    )
                    assert (document_root / "memory/.shadow/portal-config.db").is_file()
                if not enabled:
                    denied = json.loads(await provider.handle_tool_call(
                        "agent_memory_video_submit", {"uri": uri},
                    ))
                    assert denied == {"error": "Video memory tools are disabled"}
                    assert not list(tmp_path.rglob("MEMORY.md"))
                    return
                for invalid_uri, reason in (
                    (outside.as_uri(), "outside the configured import scope"),
                    (media.as_uri() + "/%2e%2e/outside.mp4", "outside the configured import scope"),
                    (other_format.as_uri(), "unsupported local video file extension"),
                ):
                    denied = json.loads(await provider.handle_tool_call(
                        "agent_memory_video_submit", {"uri": invalid_uri},
                    ))
                    assert reason in denied["error"] and "job_id" not in denied
                parameters = schemas["agent_memory_video_submit"]["parameters"]
                assert parameters["properties"]["project"]["type"] == "string"
                assert parameters["required"] == ["uri"]
                # Exercise both explicit project routing and the no-project default.
                project = (
                    {"project": "video-review"} if configuration in {"globals", "compact"} else {}
                )
                submit_args = {"uri": uri, **project}
                submission = json.loads(await provider.handle_tool_call(
                    "agent_memory_video_submit", submit_args,
                ))
                job_id = submission["job_id"]
                repeated = json.loads(await provider.handle_tool_call(
                    "agent_memory_video_submit", submit_args,
                ))
                assert repeated["job_id"] == job_id and repeated["reused"]
                async with asyncio.timeout(10):
                    while True:
                        status = json.loads(await provider.handle_tool_call(
                            "agent_memory_video_status", {"job_id": job_id},
                        ))
                        if status["status"] not in {"pending", "running"}:
                            break
                        await asyncio.sleep(0.02)
                assert status["status"] == "succeeded", status
                assert len(status["unit_ids"]) == 2
                document = json.loads(await provider.handle_tool_call(
                    "agent_memory_read_md_file", {"md_filename": "MEMORY.md", **project},
                ))
                assert {block["item_id"] for block in document["blocks"]} == set(status["unit_ids"])
                md_path = next(tmp_path.rglob("MEMORY.md"))
                assert md_path.parent.name == project.get("project", "memory")
                if configuration.startswith("file-"):
                    assert md_path.is_relative_to(tmp_path / "file-memory")
                markdown = md_path.read_text(encoding="utf-8")
                assert "A corgi chases a ball" in markdown

                def conflict(*_args, **_kwargs):
                    raise ConflictError(
                        "ingest_payload", "fixture", f"previous ingest job {job_id}",
                    )

                monkeypatch.setattr(provider._client._api, "submit_ingest", conflict)
                blocked = json.loads(await provider.handle_tool_call(
                    "agent_memory_video_submit", submit_args,
                ))
                assert blocked["code"] == "video_ingest_conflict"
                assert job_id in blocked["error"] and "reconcile" in blocked["error"]
                assert "agent_memory_video_status" in blocked["error"]
                assert "new file URI" in blocked["error"]
            finally:
                await provider.shutdown()

        asyncio.run(run())

    def test_conclude_then_search_tool_roundtrip(self):
        async def _run() -> None:
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-unit-user"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-unit-user", scope_id="tenant-sdk")
            try:
                result = json.loads(
                    await provider.handle_tool_call(
                        "agent_memory_conclude", {"conclusion": "用户偏好深色主题"}
                    )
                )
                assert result["result"] == "Fact stored."
                assert result["item_id"]

                client = provider._client
                assert provider.runtime is client._runtime
                assert provider.runtime.api is client._api
                scope = client._to_api_scope(provider.bound_scope())
                listed = await asyncio.to_thread(
                    client._api.list,
                    scope,
                    security=client._security(),
                    filters={"system_metadata.source_app": "jiuwenswarm"},
                )
                assert any(
                    unit.system_metadata.get("agent_label") == "jiuwenswarm"
                    and "深色主题" in unit.content
                    for unit in listed.items
                ), "conclude 应写入可按来源筛选的派生记忆"

                result = json.loads(
                    await provider.handle_tool_call("agent_memory_search", {"query": "深色主题"})
                )
                assert any("深色主题" in item["memory"] for item in result["results"]), result
                from jiuwen_memory.api import Context

                personal_page = await asyncio.to_thread(
                    client._api.list, security=client._security(),
                    extensions={"personal": {"source": "jiuwenswarm"}},
                )
                assert any("深色主题" in unit.content for unit in personal_page.items)
                context = Context(extensions={"personal": {}})
                personal_hits = await asyncio.to_thread(
                    client._api.search, "深色主题", context, security=client._security(),
                )
                assert any("深色主题" in hit.item.content for hit in personal_hits.items)
                assert all(hit.scope == scope for hit in personal_hits.items)
            finally:
                await provider.shutdown()

        asyncio.run(_run())

    def test_sync_turn_with_envelope_neither_raises_nor_stores_shell(self):
        async def _run() -> None:
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-envelope-user"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-envelope-user", scope_id="tenant-sdk")
            try:
                # 只验证不抛错与不崩溃——infer 派生质量取决于装配的 extractor，
                # 契约层面的完整断言在集成测试（真实服务同栈行为已实测）。
                await provider.sync_turn(ENVELOPE_ZH, "好的，已记住。")
                assert provider.is_initialized
            finally:
                await provider.shutdown()

        asyncio.run(_run())

    def test_sdk_add_returns_add_result_with_rebuilt_content(self):
        async def _run() -> None:
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-add-user"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-add-user", scope_id="tenant-sdk")
            try:
                client = provider._client
                result = await client.add("SDK 原文直写", provider.bound_scope(), tags=["t"])
                assert result is not None
                assert result.item_id
                assert result.content == "SDK 原文直写"
            finally:
                await provider.shutdown()

        asyncio.run(_run())

    def test_experience_enabled_sdk_assembles_and_tool_roundtrip(self):
        """开启形态（experience_space 非空）的真实进程内装配链路（2026-10-27）。

        缺省翻转（「配置了才启用」）后 TestSdkMode 默认构造走关闭形态，开启
        形态的「经验算子组装配 + 查经验工具 + 经验检索」链路由此钉住——条件
        拼装把经验段拼坏会在装配期或检索期炸。
        """

        async def _run() -> None:
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-exp-user"), role=Role.ROOT
                ),
                experience_space="shared-experience",
            )
            await provider.initialize(user_id="sdk-exp-user", scope_id="tenant-sdk")
            try:
                names = [s["name"] for s in provider.get_tool_schemas()]
                assert "agent_memory_experience" in names, "开启形态注册查经验工具"
                # initialize 已过 _setup_experience_jobs（restore_dreaming +
                # register_consolidation，失败按设计只记日志）；空库检索走
                # retriever.experience 全链（经验数据面 + 三段过滤），无结果容错。
                result = json.loads(
                    await provider.handle_tool_call(
                        "agent_memory_experience",
                        {"query": "部署经验", "category": "other"},
                    )
                )
                assert result["result"] == "No relevant experience found.", result
            finally:
                await provider.shutdown()

        asyncio.run(_run())


    def test_prefetch_forwards_project_to_search_filters(self):
        async def _run() -> None:
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-prefetch-user"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-prefetch-user", scope_id="tenant-sdk")
            try:
                seen: list[dict | None] = []
                original = provider._client.search

                async def _spy(query, scope, *, top_k=10, filters=None):
                    seen.append(filters)
                    return await original(query, scope, top_k=top_k, filters=filters)

                provider._client.search = _spy
                await provider.prefetch("部署区域", project="proj-a")
                await provider.prefetch("部署区域", project="  ")
                await provider.prefetch("部署区域")
                assert seen[0]["system_metadata.project"] == {"eq": "proj-a"}, seen[0]
                assert "system_metadata.project" not in seen[1], seen[1]
                assert "system_metadata.project" not in seen[2], seen[2]
            finally:
                await provider.shutdown()

        asyncio.run(_run())

    def test_prefetch_recalls_project_memory_only_in_same_project(self):
        async def _run() -> None:
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-project-user"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-project-user", scope_id="tenant-sdk")
            try:
                for conclusion, project in (
                    ("项目部署区域选了杭州", "proj-a"),
                    ("通用部署区域默认上海", ""),
                ):
                    args = {"conclusion": conclusion}
                    if project:
                        args["project"] = project
                    stored = json.loads(
                        await provider.handle_tool_call("agent_memory_conclude", args)
                    )
                    assert stored.get("result") == "Fact stored.", stored

                same = await provider.prefetch("部署区域", project="proj-a")
                assert "杭州" in same, f"同项目会话应召回项目内记忆: {same!r}"
                assert "上海" not in same, f"项目会话不应混入旧空项目记忆: {same!r}"

                other = await provider.prefetch("部署区域", project="proj-b")
                assert "杭州" not in other, f"其他项目不应看到 proj-a 的记忆: {other!r}"
                assert "上海" not in other, f"其他项目不应混入旧空项目记忆: {other!r}"

                explicit = await provider.handle_tool_call(
                    "agent_memory_search", {"query": "部署区域", "project": "proj-a"}
                )
                memories = [item["memory"] for item in json.loads(explicit)["results"]]
                assert any("杭州" in item for item in memories), memories
                assert all("上海" not in item for item in memories), memories

                # 不带 project 时过滤条件里没有项目谓词，由影子索引兜底 project=''：
                # 只召回跨项目可见的记忆，不会放宽成全库召回。
                projectless = await provider.prefetch("部署区域")
                assert "杭州" not in projectless, f"无项目会话不应看到项目记忆: {projectless!r}"
                assert "上海" in projectless, f"无项目会话应召回跨项目记忆: {projectless!r}"
            finally:
                await provider.shutdown()

        asyncio.run(_run())

    @pytest.mark.parametrize("explicit_write", [False, True])
    def test_sync_turn_keeps_project_fact_out_of_unbound_prefetch(
        self, monkeypatch, tmp_path, explicit_write,
    ):
        import agent_memory_provider as adapter

        offline_config = adapter._agent_default_config

        def config():
            result = offline_config()
            result["extractor"]["document_fact"] = {"target": "document_fact"}
            return result

        path_fact = "这个项目的论文终稿放在 /work/paper/final"
        profile_fact = "用户常用邮箱是 user@example.org"

        def extract(_self, messages):
            source_id = re.search(r"\[ID: ([^\]]+)\]", messages[1].content).group(1)
            candidates = [(path_fact, True)]
            if profile_fact in messages[1].content:
                candidates.append((profile_fact, False))
            return json.dumps([
                {
                    "source_id": source_id,
                    "content": content,
                    "memory_class": "user_profile",
                    "project_fact": project_fact,
                    "tier": "semantic",
                    "confidence": 1.0,
                }
                for content, project_fact in candidates
            ])

        monkeypatch.setattr(adapter, "_agent_default_config", config)
        monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", extract)

        async def run():
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-project-user"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-project-user", scope_id="tenant-sdk")
            try:
                if explicit_write:
                    stored = json.loads(await provider.handle_tool_call(
                        "agent_memory_conclude", {"conclusion": path_fact, "project": "KVCache"},
                    ))
                    assert stored.get("result") == "Fact stored.", stored
                else:
                    await provider.sync_turn(path_fact, "记下了。", project="KVCache")
                assert path_fact in await provider.prefetch(path_fact, project="KVCache")
                assert path_fact not in await provider.prefetch(path_fact)

                await provider.sync_turn(f"{path_fact}。{profile_fact}。", "记下了。")
                unbound = await provider.prefetch(path_fact)
                assert path_fact not in unbound
                assert profile_fact in unbound
                assert path_fact not in (tmp_path / "USER.md").read_text(encoding="utf-8")
            finally:
                await provider.shutdown()

        asyncio.run(run())


_DOCUMENT_FACT = "用户喜欢园艺"


def _fact_response(messages):
    source = re.search(r"\[ID: ([^\]]+)\]", messages[1].content).group(1)
    return json.dumps([{
        "source_id": source, "content": _DOCUMENT_FACT, "project_fact": False,
        "memory_class": "user_profile", "tier": "semantic", "confidence": 1.0,
    }])


@pytest.fixture
def document_writer(monkeypatch, tmp_path):
    from jiuwen_memory.control.types import BatchWriteItem

    model = Mock(side_effect=_fact_response)
    monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", model)
    scope = ApiScope(org="tenant-sdk", user="alice")
    provider = AgentMemoryMemoryProvider(
        config_dict={"extractor": {"document_fact": {"target": "document_fact"}}},
        security_provider=_security_provider(scope, role=Role.ROOT),
    )
    with asyncio.Runner() as runner:
        runner.run(provider.initialize(user_id=scope.user, scope_id=scope.org))
        client = provider._client
        orchestrator = client._api._commands._orchestrator
        write_scope = client._to_api_scope(provider.bound_scope())

        async def write(*, continue_on_error=True):
            result = await client._api.batch_add_async(
                [BatchWriteItem(content="第一轮", scope=write_scope)], security=client._security(),
                system_metadata={"infer": "true", "pipeline": "document_fact"},
                continue_on_error=continue_on_error,
            )
            outcome, = result.outcomes
            return outcome

        try:
            yield SimpleNamespace(
                run=runner.run, write=write, sync_turn=provider.sync_turn, model=model,
                orchestrator=orchestrator, path=tmp_path / "USER.md",
                guard=partial(orchestrator._lock.guard, scope, "infer_write"),
            )
        finally:
            runner.run(provider.shutdown())


class _WriteGate:
    """在指定工作线程处暂停首笔写入，断言失败时也会放行并回收任务。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def pause(self):
        self.entered.set()
        assert self.release.wait(10), "测试未释放工作线程"

    @asynccontextmanager
    async def writing(self, operation):
        pending = asyncio.create_task(operation)
        try:
            assert await asyncio.to_thread(self.entered.wait, 3), "写入未到达预期阶段"
            yield pending
        finally:
            self.release.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("mode", ["grouped", "fail_fast", "fallback"])
def test_document_fact_slow_extract_allows_same_user_commit(document_writer, mode):
    doc = document_writer
    gate = _WriteGate()

    def extract(messages):
        if mode == "fallback" and doc.model.call_count == 1:
            raise ValueError("invalid model output")
        if not gate.entered.is_set():
            gate.pause()
        return _fact_response(messages)

    doc.model.side_effect = extract

    async def run():
        async with gate.writing(doc.write(continue_on_error=mode != "fail_fast")) as first:
            await asyncio.wait_for(doc.sync_turn("第二轮", "记下了。"), timeout=3)
            assert doc.path.read_text(encoding="utf-8").count(_DOCUMENT_FACT) == 1
        assert not first.result().error and first.result().units == []
        assert doc.path.read_text(encoding="utf-8").count(_DOCUMENT_FACT) == 1
        assert doc.orchestrator.degrade_counters().get("merge_fallback", 0) == (mode == "fallback")

    doc.run(run())


@pytest.mark.parametrize("case", ["timeout", "backend_error", "empty_candidates"])
def test_document_commit_lock_contention(monkeypatch, document_writer, case):
    from jiuwen_memory.common.errors import BackendError
    from jiuwen_memory.common.lock import LockTimeoutError

    doc = document_writer
    gate = _WriteGate()

    def extract(messages):
        gate.pause()
        return "[]" if case == "empty_candidates" else _fact_response(messages)

    doc.model.side_effect = extract
    monkeypatch.setattr(doc.orchestrator, "_lock_wait_ms", 0)

    async def run():
        async with gate.writing(doc.write()) as pending, doc.guard():
            if case == "backend_error":
                failure = AsyncMock(side_effect=BackendError("lock backend unavailable"))
                monkeypatch.setattr(doc.orchestrator._lock, "_acquire", failure)
            gate.release.set()
            outcome = await asyncio.wait_for(pending, timeout=3)
        if case == "empty_candidates":
            assert not outcome.error and not outcome.units
        else:
            error = BackendError if case == "backend_error" else LockTimeoutError
            assert isinstance(outcome.error_obj, error)
        assert doc.model.call_count == 1
        expected = {"lock_timeout": 1} if case == "timeout" else {}
        assert doc.orchestrator.degrade_counters() == expected
        assert not doc.path.exists() or _DOCUMENT_FACT not in doc.path.read_text(encoding="utf-8")

    doc.run(run())


@pytest.mark.parametrize("stage", ["admission", "commit"])
def test_document_cancel_while_waiting_for_lock(monkeypatch, document_writer, stage):
    doc = document_writer

    async def run():
        waiting, release = asyncio.Event(), asyncio.Event()
        acquire = doc.orchestrator._lock._acquire

        async def blocked_acquire(*args, **kwargs):
            if stage == "admission" or doc.model.called:
                waiting.set()
                await release.wait()
            return await acquire(*args, **kwargs)

        monkeypatch.setattr(doc.orchestrator._lock, "_acquire", blocked_acquire)
        pending = asyncio.create_task(doc.write())
        try:
            await asyncio.wait_for(waiting.wait(), timeout=3)
            pending.cancel()
            done, _ = await asyncio.wait([pending], timeout=1)
            assert pending in done and pending.cancelled()
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
        assert doc.model.call_count == (1 if stage == "commit" else 0)
        assert not doc.path.exists() or _DOCUMENT_FACT not in doc.path.read_text(encoding="utf-8")

    doc.run(run())


@pytest.mark.parametrize("continue_on_error", [True, False])
def test_document_cancel_during_commit_keeps_lock_and_records_ids(
    monkeypatch, document_writer, continue_on_error,
):
    from jiuwen_memory.common.lock import LockTimeoutError
    from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import OrchestratingEvolver
    from jiuwen_memory.control.ingest_job import IngestCancellation, ingest_cancellation

    doc = document_writer
    gate = _WriteGate()
    commit = OrchestratingEvolver._commit_candidates

    def blocked_commit(evolver, state):
        gate.pause()
        return commit(evolver, state)

    monkeypatch.setattr(OrchestratingEvolver, "_commit_candidates", blocked_commit)

    async def run():
        job = IngestCancellation(requested=threading.Event())
        token = ingest_cancellation.set(job)
        try:
            async with gate.writing(doc.write(continue_on_error=continue_on_error)) as pending:
                pending.cancel()
                await asyncio.sleep(0)  # 让第一次取消进入等待在途提交的清理分支
                pending.cancel()
                done, _ = await asyncio.wait([pending], timeout=0.1)
                assert not done
                with pytest.raises(LockTimeoutError):
                    async with doc.guard(wait_timeout_ms=0):
                        pytest.fail("工作线程仍在写入时锁已释放")
        finally:
            ingest_cancellation.reset(token)
        assert pending.cancelled() and len(job.unit_ids) == 1
        async with doc.guard(wait_timeout_ms=0):
            assert doc.path.read_text(encoding="utf-8").count(_DOCUMENT_FACT) == 1

    doc.run(run())


# --------------------------------------------------------------------------- #
# _merge_config_over_default：上层 config_dict 深合并到内置 agent 默认装配
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scene, kind, expected", [
    ("agent", "lock", "Timed out waiting for the memory write lock."),
    ("agent", "other", "Memory write failed; check the service logs for details."),
    ("team", "lock", "Timed out waiting for the memory write lock."),
])
def test_sdk_write_error_reaches_tool_without_breaking_sync(
    monkeypatch, caplog, scene, kind, expected,
):
    from jiuwen_memory.common.lock import LockTimeoutError

    async def run():
        provider = AgentMemoryMemoryProvider(
            scene=scene,
            collective_coordinates={"team": "reviewers"} if scene == "team" else None,
            security_provider=_security_provider(ApiScope(org="local", user="developer")),
        )
        await provider.initialize(user_id="developer", scope_id="local")
        try:
            error = LockTimeoutError if kind == "lock" else RuntimeError
            monkeypatch.setattr(provider._client._api, "add_async", AsyncMock(
                side_effect=error("private backend details"),
            ))
            tool = "team_memory_write" if scene == "team" else "agent_memory_conclude"
            field = "content" if scene == "team" else "conclusion"
            result = json.loads(await provider.handle_tool_call(tool, {field: "A durable fact"}))
            assert result == {"error": expected}
            caplog.clear()
            assert await provider.sync_turn("A durable fact", "Understood", sources=[{
                "id": "user", "role": "user", "text": "A durable fact",
            }]) is None
            assert "sync_turn add failed" in caplog.text
        finally:
            await provider.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("case, expected, failures", [
    ("lock", "Timed out waiting for the memory write lock.", 0),
    ("timeout", "Memory service request timed out; write outcome is unknown.", 1),
    ("connect", "Unable to connect to the memory service.", 1),
    ("http_error", "Memory write failed: HTTP 503.", 1),
    ("html_error", "Memory write failed: HTTP 502.", 1),
    ("invalid_json", "Memory write failed; write outcome is unknown.", 1),
    ("empty", "", 0),
])
@pytest.mark.parametrize("scene", ["agent", "team"])
def test_http_write_error_reaches_tool_and_preserves_breaker(
    caplog, scene, case, expected, failures,
):
    httpx = pytest.importorskip("httpx")
    from jiuwen_memory.common.lock import LockTimeoutError
    from jiuwen_memory_entry.core.error_response import error_response

    timeouts = []

    def respond(request):
        timeouts.append((request.url.path, request.extensions["timeout"]["read"]))
        if case in {"timeout", "connect"}:
            error = httpx.ReadTimeout if case == "timeout" else httpx.ConnectError
            raise error("private transport details", request=request)
        if case == "lock":
            status, body, _ = error_response(LockTimeoutError, "private lock key")
            return httpx.Response(status, json=body)
        if case == "http_error":
            return httpx.Response(503, json={"error": "BackendError"})
        if case == "html_error":
            return httpx.Response(502, text="private proxy details")
        if case == "invalid_json":
            return httpx.Response(200, text="invalid JSON")
        return httpx.Response(200, json=[])

    async def run():
        provider = AgentMemoryMemoryProvider(
            base_url="http://memory.test", scene=scene,
            collective_coordinates={"team": "reviewers"} if scene == "team" else None,
        )
        client = _HttpClient("http://memory.test")
        async with httpx.AsyncClient(
            base_url="http://memory.test", transport=httpx.MockTransport(respond),
        ) as http:
            client._http = http
            client._is_initialized = True
            provider._client = client
            provider._initialized = True
            tool = "team_memory_write" if scene == "team" else "agent_memory_conclude"
            field = "content" if scene == "team" else "conclusion"
            result = json.loads(await provider.handle_tool_call(tool, {field: "A durable fact"}))
            if expected:
                assert result == {"error": expected}
            elif scene == "team":
                assert result == {"stored": False, "item_id": None}
            else:
                assert result == {"result": "Fact stored (deduped, no new unit)."}
            assert client._consecutive_failures == failures
            assert timeouts == [("/v1/add", 300.0)]
            if case in {"timeout", "connect", "http_error", "html_error", "invalid_json"}:
                assert await client._post_verb("search", {}) is None
                assert timeouts[-1] == ("/v1/search", 30.0)
            if expected:
                caplog.clear()
                assert await provider.sync_turn("A durable fact", "Understood", sources=[{
                    "id": "user", "role": "user", "text": "A durable fact",
                }]) is None
                assert "sync_turn add failed" in caplog.text

    asyncio.run(run())


@pytest.mark.parametrize("override, expected_ms", [(None, 130_000), (0, 0), (45_000, 45_000)])
def test_sdk_write_lock_budget_is_assembled_and_overridable(override, expected_ms):
    import agent_memory_provider as adapter

    supplied = {} if override is None else {
        "write": {"default": {"params": {"lock_wait_ms": override}}},
    }
    client = _InProcessClient(
        None,
        config_dict=adapter._merge_config_over_default(supplied),
        security_provider=_security_provider(ApiScope(org="local", user="developer")),
    )
    try:
        commands = private(private(client, "_api"), "_commands")
        writer = private(commands, "_orchestrator")
        assert private(writer, "_lock_wait_ms") == expected_ms
        assert private(private(writer, "_lock"), "_wait_timeout_ms") == 10_000
    finally:
        asyncio.run(client.close())


class TestMergeConfigOverDefault:
    @pytest.mark.parametrize("source", ["globals", "env"])
    def test_video_model_urls_and_names_are_trimmed(self, tmp_path, monkeypatch, source):
        import agent_memory_provider as _amp

        settings = {"local_media_roots": [{"scope": {"org": "tenant"}, "root": str(tmp_path)}]}
        inputs = [
            ("asr_base_url", "ASR_API_BASE", "https://asr.example.invalid/v1"),
            ("asr_model", "ASR_MODEL_NAME", "asr-model"),
            ("video_base_url", "VIDEO_API_BASE", "https://vision.example.invalid/v1"),
            ("video_model", "VIDEO_MODEL_NAME", "vision-model"),
        ]
        for key, env_name, value in inputs:
            if source == "env":
                monkeypatch.setenv(env_name, f"  {value}  ")
            else:
                settings[key] = f"  {value}  "

        merged = _amp._merge_config_over_default({"globals": settings})
        asr = merged["asr"]["video"]["params"]
        vision = merged["llm"]["video_vision"]["params"]
        assert asr["asr_base_url"] == "https://asr.example.invalid/v1"
        assert asr["asr_model"] == "asr-model"
        assert vision["llm_base_url"] == "https://vision.example.invalid/v1"
        assert vision["llm_model"] == "vision-model"

    def test_compact_video_overrides_env_and_preserves_input(self, tmp_path, monkeypatch):
        import copy

        import agent_memory_provider as _amp

        env_root = tmp_path / "env-imports"
        env_root.mkdir()
        monkeypatch.setenv("JIUWEN_MEMORY_VIDEO_ROOTS", json.dumps([
            {"scope": {"org": "tenant"}, "root": str(env_root)},
        ]))
        for name, value in {
            "ASR_API_BASE": "https://env-asr.example.invalid/v1",
            "ASR_MODEL_NAME": "env-asr-model",
            "ASR_API_KEY": "env-asr-key",
            "VIDEO_API_BASE": "https://env-vision.example.invalid/v1",
            "VIDEO_MODEL_NAME": "env-vision-model",
            "VIDEO_API_KEY": "env-vision-key",
        }.items():
            monkeypatch.setenv(name, value)
        roots = [{"scope": {"org": "tenant"}, "root": str(tmp_path)}]
        supplied = {"globals": {
            "local_media_roots": roots,
            "asr_base_url": "https://portal-asr.example.invalid/v1",
            "asr_model": "portal-asr",
            "video_base_url": "https://portal-video.example.invalid/v1",
            "video_model": "portal-vision",
            "video_api_key": "",
            "asr_api_key": None,
        }}
        original = copy.deepcopy(supplied)
        merged = _amp._merge_config_over_default(supplied)
        routes = merged["normalizer"]["default"]["params"]["routes"]
        assert routes["video"]["params"]["local_media_roots"] == roots
        assert merged["asr"]["video"]["params"] == {
            "asr_base_url": "https://portal-asr.example.invalid/v1",
            "asr_model": "portal-asr",
            "asr_api_key": "env-asr-key",
        }
        assert merged["llm"]["video_vision"]["params"] == {
            "llm_base_url": "https://portal-video.example.invalid/v1",
            "llm_model": "portal-vision",
            "llm_api_key": "env-vision-key",
            "llm_temperature": 0.0,
        }
        assert merged["pipeline"]["default"]["params"]["profiles"]["video"]["evolver"] == "video"
        assert supplied == original

        with_explicit_component = _amp._merge_config_over_default({
            **supplied, "asr": {"video": {"params": {"asr_model": "explicit-instance"}}},
        })
        assert with_explicit_component["asr"]["video"]["params"]["asr_model"] == "explicit-instance"

    def test_globals_creds_override_preserves_agent_stack(self, monkeypatch):
        import agent_memory_provider as _amp

        monkeypatch.setattr(_amp, "_agent_default_config", lambda *a, **k: {
            "globals": {"write_document": True, "embedder_dim": 1024, "llm_api_key": ""},
            "markdown_store": {"default": {"target": "local", "params": {"root": ""}}},
            "engine": {"default": {"target": "agent"}},
        })
        merged = _amp._merge_config_over_default({
            "globals": {"llm_api_key": "sk-cred", "embedder_dim": 64},
            "markdown_store": {"default": {"params": {"root": "/data/md"}}},
        })
        assert merged["globals"]["llm_api_key"] == "sk-cred", "globals 凭证键覆盖"
        assert merged["globals"]["embedder_dim"] == 64, "globals.embedder_dim 覆盖"
        assert merged["globals"]["write_document"] is True, "未提及的 globals 键保留"
        md_default = merged["markdown_store"]["default"]
        assert md_default["target"] == "local", "只改 root 不应丢 target"
        assert md_default["params"]["root"] == "/data/md", "params.root 覆盖"
        assert merged["engine"]["default"]["target"] == "agent", "未提及 namespace 保留"

    def test_agent_default_dedup_isolates_by_tier(self):
        """agent 默认装配的去重只与同 tier 的记忆比较。

        抽取会为可复用标识另出一条 semantic 稳定事实；不隔离时它会被同批的
        episodic 事件判重丢弃。
        """
        import agent_memory_provider as _amp

        params = _amp._agent_default_config()["dedup"]["default"]["params"]
        assert params.get("dedup_tier_filter") is True, params
        assert params.get("embedder") == "default", params

    def test_each_call_returns_fresh_dict(self, monkeypatch):
        import agent_memory_provider as _amp

        monkeypatch.setattr(_amp, "_agent_default_config", lambda *a, **k: {"globals": {"a": 1}})
        first = _amp._merge_config_over_default({"globals": {"b": 2}})
        second = _amp._merge_config_over_default({"globals": {"c": 3}})
        assert first is not second, "每次返回全新字面量"
        assert first["globals"] == {"a": 1, "b": 2}, "首次合并结果"
        assert second["globals"] == {"a": 1, "c": 3}, "无跨调用可变状态污染"


def test_source_metadata_added_to_each_provider_write_path(monkeypatch):
    import agent_memory_provider as provider_module

    async def run():
        provider = AgentMemoryMemoryProvider(agent_id="writer-agent", portal={"enabled": False})
        client = SimpleNamespace(
            initialize=AsyncMock(),
            close=AsyncMock(),
            is_unavailable=Mock(return_value=False),
            add=AsyncMock(return_value=SimpleNamespace(item_id="unit")),
        )
        monkeypatch.setattr(provider_module, "_build_client", Mock(return_value=client))
        await provider.initialize()
        try:
            await provider.handle_tool_call("agent_memory_conclude", {"conclusion": "first"})
            await provider.handle_tool_call("agent_memory_procedural", {"content": "second"})
            await provider.sync_turn("third", "fourth")
            assert client.add.await_count == 3
            for call in client.add.await_args_list:
                metadata = call.kwargs["system_metadata"]
                assert metadata["source_app"] == "jiuwenswarm"
                assert metadata["agent_label"] == "writer-agent"
        finally:
            await provider.shutdown()

    asyncio.run(run())


def test_sync_turn_writes_filterable_source_metadata():
    async def run():
        provider = AgentMemoryMemoryProvider(
            agent_id="assistant-one",
            security_provider=_security_provider(
                ApiScope(org="tenant-sdk", user="source-test"), role=Role.ROOT
            ),
        )
        await provider.initialize(user_id="source-test", scope_id="tenant-sdk")
        try:
            await provider.sync_turn("我的饮食偏好是素食", "我会记住。")
            client = provider._client
            result = await asyncio.to_thread(
                client._api.list,
                client._to_api_scope(provider.bound_scope()),
                security=client._security(),
                filters={"system_metadata.source_app": "jiuwenswarm"},
            )
            assert any(
                unit.system_metadata.get("agent_label") == "assistant-one"
                and "素食" in unit.content
                for unit in result.items
            )
        finally:
            await provider.shutdown()

    asyncio.run(run())


def test_personal_memory_spans_agents_and_locates_hits():
    from jiuwen_memory.api import Context

    async def run():
        provider = AgentMemoryMemoryProvider(
            security_provider=_security_provider(
                ApiScope(org="tenant-sdk", user="aggregate-user"), role=Role.ROOT
            ),
        )
        await provider.initialize(user_id="aggregate-user", scope_id="tenant-sdk")
        try:
            client = provider._client
            first = client._to_api_scope(provider.bound_scope())
            second = ApiScope(org=first.org, user=first.user, agent="another-agent")
            await client._api.add_async(
                "海边旅行计划", first, security=client._security(),
                system_metadata={"source_app": "first"},
            )
            await client._api.add_async(
                "海边度假计划", second, security=client._security(),
                system_metadata={"source_app": "second"},
            )
            extensions = {"personal": {}}
            page = await asyncio.to_thread(
                client._api.list, security=client._security(), extensions=extensions,
            )
            assert {unit.system_metadata.get("source_app") for unit in page.items} >= {
                "first", "second"
            }
            result = await asyncio.to_thread(
                client._api.search, "海边", Context(extensions=extensions),
                security=client._security(),
            )
            assert {hit.scope.agent for hit in result.items} >= {first.agent, second.agent}
        finally:
            await provider.shutdown()

    asyncio.run(run())


def test_personal_server_automatically_uses_existing_document_root(tmp_path):
    import agent_memory_provider as provider_module

    from jiuwen_memory_entry.core.profiles import load_config
    from jiuwen_memory_entry.http_server.__main__ import HttpServer
    from jiuwen_memory_entry.http_server.dev_security import build_dev_security_runtime

    config = load_config([{"memory_api": provider_module._agent_default_config()}])
    server = HttpServer.build_personal(
        config,
        llm_instances=["default"],
        authorize=lambda security, operation: True,
        security_runtime=build_dev_security_runtime(),
    )
    try:
        assert server.portal_enabled
        assert server._runtime.configuration is not None
        assert (tmp_path / "memory" / ".shadow" / "portal-config.db").is_file()
        result = server._runtime.configuration.save(
            0, {"llm.default.params.llm_model": "persisted"}, security=object()
        )
        assert result["version"] == 1
    finally:
        server.close(wait=True)
    restarted = HttpServer.build_personal(
        config,
        llm_instances=["default"],
        authorize=lambda security, operation: True,
        security_runtime=build_dev_security_runtime(),
    )
    try:
        settings = restarted._runtime.configuration.read(security=object())
        assert settings["version"] == 1
        assert any(field["saved"] == "persisted" for field in settings["fields"])
    finally:
        restarted.close(wait=True)


def test_discovered_personal_memory_filters_shared_scope_by_trusted_author():
    from jiuwen_memory.api import Context

    async def run():
        actor = ApiScope(org="tenant-sdk", user="aggregate-user")
        provider = AgentMemoryMemoryProvider(
            security_provider=_security_provider(actor, role=Role.ROOT),
        )
        await provider.initialize(user_id="aggregate-user", scope_id="tenant-sdk")
        try:
            client = provider._client
            own = ApiScope(org=actor.org, user=actor.user, agent="one")
            second = ApiScope(org=actor.org, user=actor.user, agent="two")
            shared = ApiScope(org=actor.org, agent="shared")
            other = ApiScope(org=actor.org, user="another")
            for scope, content in [(own, "个人记忆一"), (second, "个人记忆二"),
                                   (shared, "共享范围我的记忆"), (other, "其他范围记忆")]:
                await client._api.add_async(content, scope, security=client._security())
            other_security = _security_provider(
                ApiScope(org=actor.org, user="another"), role=Role.ROOT
            )()
            await client._api.add_async("共享范围他人记忆", shared, security=other_security)
            extensions = {"personal": {}}
            page = await asyncio.to_thread(
                client._api.list, security=client._security(), extensions=extensions,
            )
            contents = [unit.content for unit in page.items]
            assert any("个人记忆一" in item for item in contents)
            assert any("个人记忆二" in item for item in contents)
            assert any("共享范围我的记忆" in item for item in contents)
            assert not any(
                "其他范围记忆" in item or "共享范围他人记忆" in item for item in contents
            )
            result = await asyncio.to_thread(
                client._api.search, "记忆", Context(extensions=extensions),
                security=client._security(),
            )
            assert not any("共享范围他人记忆" in hit.item.content for hit in result.items)
        finally:
            await provider.shutdown()

    asyncio.run(run())


def test_scope_discovery_rejects_untrusted_context_before_store_access():
    from jiuwen_memory.api import PermissionDeniedError, RequestSecurityContext

    async def run():
        actor = ApiScope(org="tenant-sdk", user="source-test")
        provider = AgentMemoryMemoryProvider(
            security_provider=_security_provider(actor, role=Role.ROOT)
        )
        await provider.initialize(user_id="source-test", scope_id="tenant-sdk")
        try:
            client = provider._client
            original = client._api._queries.candidate_scopes

            def fail_if_accessed():
                raise AssertionError("untrusted context reached storage")

            client._api._queries.candidate_scopes = fail_if_accessed
            try:
                forged = RequestSecurityContext(auth=AuthContext(actor=actor))
                with pytest.raises(PermissionDeniedError):
                    await asyncio.to_thread(
                        client._api.list, security=forged, extensions={"personal": {}},
                    )
            finally:
                client._api._queries.candidate_scopes = original
        finally:
            await provider.shutdown()

    asyncio.run(run())


def test_default_dev_server_auto_manages_existing_document_config(tmp_path, monkeypatch):
    import agent_memory_provider as provider_module

    from jiuwen_memory.api import Credentials
    from jiuwen_memory.construction.prompt_registry import PromptRegistry
    from jiuwen_memory_entry.core.dev_security import with_local_dev_security
    from jiuwen_memory_entry.core.profiles import load_config
    from jiuwen_memory_entry.http_server.__main__ import HttpServer

    settings = provider_module._agent_default_config()
    settings["extractor"] = {
        "default": {"target": "dynamic_llm", "params": {"llm": "default", "fallback": "legacy"}},
        "legacy": {"target": "keyword", "params": {"chunker": "default"}},
    }
    settings["prompts"] = {"extract": {"fact": "original"}}
    config = with_local_dev_security(load_config([{"memory_api": settings}]))
    server = HttpServer.build(config)
    try:
        assert server.portal_enabled
        assert server._runtime.configuration is not None
        assert (tmp_path / "memory" / ".shadow" / "portal-config.db").is_file()
        security = server._runtime.authenticate(Credentials())
        from jiuwen_memory.api import PermissionDeniedError, RequestSecurityContext

        forged = RequestSecurityContext(auth=AuthContext(actor=security.auth.actor))
        with pytest.raises(PermissionDeniedError):
            server._runtime.configuration.save(
                0, {"llm.default.params.llm_model": "forged"}, security=forged
            )
        settings = server.configuration.read(security=security)
        fields = {field["key"]: field for field in settings["fields"]}
        assert fields["llm.default.target"]["writable"]
        assert "prompts.extract.fact" not in fields
        source = server._runtime._runtime._kernel.config_source
        assert PromptRegistry(config_source=source).get("extract", "fact") == "original"
        result = server._runtime.configuration.save(
            0, {"llm.default.params.llm_model": "changed"}, security=security
        )
        assert result["applied"]
        assert PromptRegistry(config_source=source).get("extract", "fact") == "original"
        from jiuwen_memory.common.errors import BackendError

        def reject_unhealthy_audit():
            raise BackendError("audit unavailable")

        monkeypatch.setattr(server.api, "_ensure_audit_integrity_healthy", reject_unhealthy_audit)
        with pytest.raises(BackendError):
            server._runtime.configuration.save(
                1, {"llm.default.params.llm_model": "must not save"}, security=security
            )
        assert server._runtime.configuration.read(security=security)["version"] == 1
        assert PromptRegistry(config_source=source).get("extract", "fact") == "original"
    finally:
        server.close(wait=True)


class _FakeExperienceClient:
    """经验链路的桩 client：记录调用参数、按预设返回/抛错，不装配内核。

    只实现 provider 经验分支触到的方法面（handle_tool_call 的前置 guard 需要
    is_unavailable；initialize 真实路径需要 initialize/close 空实现）。
    """

    def __init__(self, *, skills=None, search_items=None, error=None):
        self.add_calls: list[tuple[str, object, dict | None]] = []
        self.search_calls: list[dict] = []
        self.consolidation_calls: list[tuple[object, int]] = []
        self.restore_calls = 0
        self._skills = skills or []
        self._search_items = search_items or []
        self._error = error

    def is_unavailable(self) -> bool:
        return False

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def add_trajectory(self, content, scope, *, system_metadata=None):
        if self._error is not None:
            raise self._error
        self.add_calls.append((content, scope, system_metadata))

    async def search_experience(
        self, query, scope, *, category, capabilities, top_k=5
    ):
        self.search_calls.append(
            {
                "query": query,
                "scope": scope,
                "category": category,
                "capabilities": list(capabilities),
                "top_k": top_k,
            }
        )
        if self._error is not None:
            raise self._error
        return self._search_items

    async def list_published_skills(self, scope):
        if self._error is not None:
            raise self._error
        return self._skills

    async def restore_dreaming(self) -> bool:
        self.restore_calls += 1
        return True

    async def register_consolidation(self, scope, interval):
        self.consolidation_calls.append((scope, interval))


def _experience_provider(**kwargs) -> AgentMemoryMemoryProvider:
    """预置桩 client 的 provider：不走 _build_client，直接测 provider 层行为。

    经验能力显式开（构造缺省已是空串=关闭，对齐方案 §2.3）；测试关闭形态时
    显式传 ``experience_space=""`` 覆盖。
    """
    kwargs.setdefault("experience_space", "shared-experience")
    provider = AgentMemoryMemoryProvider(**kwargs)
    provider._client = _FakeExperienceClient()
    provider._initialized = True
    return provider


class TestExperienceDefaultConfig:
    """内置默认装配的经验段结构（经 _offline 包装仍保留：只改 llm/embedder 等
    连接键，不动 pipeline/evolver/domain_stores 结构，见 autouse fixture）。
    """

    def test_experience_profile_wired_into_pipeline(self):
        import agent_memory_provider as _amp

        cfg = _amp._agent_default_config(experience_space="shared-experience")
        profiles = cfg["pipeline"]["default"]["params"]["profiles"]
        assert set(profiles) == {"default", "experience"}, "metadata 双 profile"
        exp = profiles["experience"]
        assert exp["index_builder"] == "experience", "经验索引算子指名"
        assert exp["retriever"] == "experience", "经验检索算子指名"
        assert exp["evolver"] == "experience", "经验演进算子指名"
        assert cfg["pipeline"]["default"]["params"]["fallback"] == "default", (
            "对话单元无路由值时回落 default（行为不变）"
        )

    def test_experience_mode_explicitly_binds_kv_domain_and_periodic_operators(self):
        import agent_memory_provider as _amp

        cfg = _amp._agent_default_config(experience_space="shared-experience")
        assert cfg["engine"]["default"]["params"]["mode_evolvers"] == {
            "experience_consolidate": {
                "evolver": "experience", "domain_store": "experience", "retriever": "experience",
            },
        }, "经验周期任务显式读经验数据面，不依赖文档list合并KV"
        params = cfg["evolver"]["experience"]["params"]
        assert params["curator"] == "experience", "周期整理依赖具名接线"
        assert params["abstractor"] == "experience_skill", "技能归纳依赖具名接线"
        assert cfg["curator"]["experience"]["params"]["index_builder"] == "experience"
        assert cfg["abstractor"]["experience_skill"]["target"] == "skill"

    def test_document_skill_files_do_not_configure_a_skill_database(self, tmp_path):
        import agent_memory_provider as _amp

        cfg = _amp._merge_config_over_default(
            {"kv_store": {"default": "redis"}}, experience_space="shared-experience",
            skills_dir=str(tmp_path), skill_capabilities=["bash"],
        )
        domains = cfg["store_manager"]["default"]["params"]["domain_stores"]
        assert cfg["constructor"]["experience"]["params"]["skill_files"] == {
            "root": str(tmp_path), "capabilities": ["bash"],
        }
        assert "experience_skill" not in cfg["constructor"]
        assert domains["experience"]["kv_store"] == "default"
        assert "experience_skills" not in cfg["kv_store"]

    @pytest.mark.parametrize("skill_enabled", [True, False])
    @pytest.mark.parametrize("write_document", [False, True])
    def test_experience_mode_reads_only_its_candidates(
        self, monkeypatch, tmp_path, skill_enabled, write_document,
    ):
        from jiuwen_memory.common.type_def import (
            MemoryTier,
            MemoryUnit,
            Scope,
            Segment,
            Temporal,
            memory_key,
        )
        from jiuwen_memory.common.type_def.memory_codec import dumps
        from jiuwen_memory.construction import EvolveMode
        from jiuwen_memory.construction.curator import CurateOutcome
        from jiuwen_memory.control.types import JobStatus, MemoryPatch, UpdateMode

        async def run():
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="mode-test", user="owner"), role=Role.ROOT,
                ),
                experience_space="mode-experience",
                config_dict={
                    "globals": {"experience": {"skill": skill_enabled}},
                    "markdown_store": {"default": {"params": {"root": str(tmp_path)}}},
                    "retriever": {"experience": {"params": {"write_document": write_document}}},
                },
            )
            await provider.initialize(user_id="owner", scope_id="mode-test")
            try:
                engine = provider._client._api._engine
                scope = Scope(org="mode-test", space="mode-experience")
                ordinary_scope = Scope(org="mode-test", space="mode-ordinary")
                ordinary = MemoryUnit(
                    id="document", scope=ordinary_scope, segments=[Segment(content="普通文档记忆")],
                    system_metadata={"memory_class": "long_term_memory"},
                )
                engine._domain_store.add(ordinary_scope, [ordinary])
                experience = MemoryUnit(
                    id="experience", scope=scope, tier=MemoryTier.SEMANTIC,
                    segments=[Segment(content="可复用经验")],
                    temporal=Temporal(t_ingest=datetime.now(UTC)),
                    system_metadata={"memory_type": "experience", "category": "other"},
                )
                if write_document:
                    engine._index_for_unit(experience).build([experience])
                else:
                    engine.kv.insert(scope, memory_key(experience.id), dumps(experience))
                skill = MemoryUnit(
                    id="published-skill", scope=scope, tags=["approved"],
                    segments=[Segment(content="Published SKILL.md")],
                    system_metadata={"memory_type": "skill", "skill_name": "published-skill"},
                )
                binding = private(engine, "_mode_evolvers")[EvolveMode.EXPERIENCE_CONSOLIDATE]
                if write_document:
                    private(binding.evolver, "_index").build([skill])
                    assert (await engine.list(scope, memory_types=["skill"])).items == []
                else:
                    engine.kv.insert(scope, memory_key(skill.id), dumps(skill))
                    listed = await engine.list(scope, memory_types=["skill"])
                    assert [u.id for u in listed.items] == [skill.id]
                    client = private(provider, "_client")
                    published = await client.list_published_skills(
                        private_method(provider, "_experience_scope")(),
                    )
                    assert [item["item_id"] for item in published] == [skill.id]
                    updated = await engine.update(
                        skill.id, scope,
                        MemoryPatch(content="Revised skill", mode=UpdateMode.OVERWRITE),
                    )
                    assert (await engine.get(updated.id, scope)).content == "Revised skill"
                assert (await engine.get(experience.id, scope)).id == experience.id
                assert engine._domain_store.get(scope, [skill.id]) == []
                document_ids = [u.id for u in engine._domain_store.list(ordinary_scope).items]
                assert document_ids == ["document"], (
                    "原文档list仍只列文档，不合并KV"
                )
                assert scope not in engine.candidate_scopes(), "普通文档数据面不存在经验桶"
                assert scope in engine.candidate_scopes_for_mode(EvolveMode.EXPERIENCE_CONSOLIDATE)
                seen = []

                def record(units):
                    seen.extend(unit.id for unit in units)
                    return CurateOutcome([] if skill_enabled else list(units), succeeded=True)

                def forbidden_generation(*_args, **_kwargs):
                    raise AssertionError("关闭技能时不应进入技能生成阶段")

                monkeypatch.setattr(binding.evolver._curator, "curate_with_status", record)
                monkeypatch.setattr(
                    binding.evolver._abstractor, "_generate_group", forbidden_generation,
                )
                job_id = await engine.evolve(scope, EvolveMode.EXPERIENCE_CONSOLIDATE)
                async with asyncio.timeout(5):
                    while engine._scheduler.status(job_id).status in (
                        JobStatus.PENDING, JobStatus.RUNNING,
                    ):
                        await asyncio.sleep(0.01)
                assert engine._scheduler.status(job_id).status is JobStatus.SUCCEEDED
                assert seen == ["experience"], "专用 mode 只从经验数据面取候选"

                async def consolidate_again():
                    job_id = await engine.evolve(scope, EvolveMode.EXPERIENCE_CONSOLIDATE)
                    async with asyncio.timeout(5):
                        while engine._scheduler.status(job_id).status in (
                            JobStatus.PENDING, JobStatus.RUNNING,
                        ):
                            await asyncio.sleep(0.01)
                    assert engine._scheduler.status(job_id).status is JobStatus.SUCCEEDED
                await consolidate_again()
                assert seen == ["experience"], "无新增时不重复整理"
                from copy import deepcopy
                added = deepcopy(experience)
                added.id = "new-experience"
                if write_document:
                    engine._index_for_unit(added).build([added])
                else:
                    engine.kv.insert(scope, memory_key(added.id), dumps(added))
                await consolidate_again()
                assert sorted(seen) == [
                    "experience", "experience", "new-experience",
                ], "新增触发后旧经验仍参与"

            finally:
                await provider.shutdown()

        asyncio.run(run())

    def test_experience_document_rejects_conflicting_configuration(self):
        import agent_memory_provider as provider

        with pytest.raises(provider.ValidationError, match="Conflicting experience"):
            provider._merge_config_over_default(
                {
                    "store_manager": {"default": {"params": {"domain_stores": {
                        "experience": {"write_document": False},
                    }}}},
                    "retriever": {"experience": {"params": {"write_document": True}}},
                },
                experience_space="shared",
            )
        with pytest.raises(provider.ValidationError, match="Incompatible experience constructor"):
            provider._merge_config_over_default(
                {"constructor": {"experience": "hybrid"}}, experience_space="shared",
            )

    def test_document_skill_is_saved_as_file_at_build_and_survives_restart(self, tmp_path):
        from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
        from jiuwen_memory.construction import EvolveMode

        scope = Scope(org="skills-test", space="shared")
        config = {"markdown_store": {"default": {"params": {"root": str(tmp_path)}}}}
        skills_dir = tmp_path / "skills"
        target = skills_dir / "distilled" / "ci-check" / "SKILL.md"
        author_key_file = tmp_path / "swarm/config/.experience-author.key"

        def make_provider():
            return AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org=scope.org, user="owner"), role=Role.ROOT,
                ),
                experience_space=scope.space,
                config_dict=config,
                register_experience_jobs=False,
                skills_dir=str(skills_dir),
            )

        def experience_evolver(provider):
            client = private(provider, "_client")
            engine = private(private(client, "_api"), "_engine")
            binding = private(engine, "_mode_evolvers")[EvolveMode.EXPERIENCE_CONSOLIDATE]
            return engine, binding.evolver

        async def run():
            first = make_provider()
            await first.initialize(user_id="owner", scope_id=scope.org)
            try:
                engine, evolver = experience_evolver(first)
                author_key = author_key_file.read_text(encoding="ascii").strip()
                assert len(author_key) == 64
                assert private(private(evolver, "_appraiser"), "_author_key") == author_key
                skills = [
                    MemoryUnit(
                        id=f"skill-{tag}", scope=scope, tags=[tag],
                        segments=[Segment(content=f"Check CI, {tag}")],
                        system_metadata={"memory_type": "skill", "skill_name": "ci-check"},
                    )
                    for tag in ("draft", "trial", "approved")
                ]
                private(evolver, "_index").build(skills)
                domain = private_method(engine, "_experience_domain")()
                shadow = private_method(domain, "_raw_shadow_index")()
                assert shadow.list_units(scope) == []
                assert not engine.kv.list(scope, memory_types=["skill"]).entries
                assert target.read_text(encoding="utf-8") == "Check CI, trial"
                assert list(target.parent.iterdir()) == [target]
                assert not (tmp_path / "memory/experience/skills.db").exists()
            finally:
                await first.shutdown()

            second = make_provider()
            await second.initialize(user_id="owner", scope_id=scope.org)
            try:
                assert author_key_file.read_text(encoding="ascii").strip() == author_key
                _, evolver = experience_evolver(second)
                assert private(private(evolver, "_appraiser"), "_author_key") == author_key
                assert target.read_text(encoding="utf-8") == "Check CI, trial"
            finally:
                await second.shutdown()

        asyncio.run(run())

    def test_experience_document_writes_through_provider_binding(self, monkeypatch):
        from dataclasses import replace

        from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
        from jiuwen_memory.construction import EvolveMode
        from jiuwen_memory.control.types import DeleteMode, DeleteSelector, MemoryPatch, UpdateMode
        from jiuwen_memory.storage.markdown_impl.experience_blocks import DOCUMENT_PATH
        from jiuwen_memory.storage.watchdog_impl.local_watchdog import LocalWatchdog

        async def run():
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="doc-tenant", user="owner"), role=Role.ROOT,
                ),
                experience_space="doc-experience",
            )
            await provider.initialize(user_id="owner", scope_id="doc-tenant")
            try:
                engine = provider._client._api._engine
                scope = Scope(org="doc-tenant", space="doc-experience")
                unit = MemoryUnit(
                    id="document-experience", scope=scope,
                    segments=[Segment(content="verified deployment steps")],
                    system_metadata={"memory_type": "experience", "category": "other"},
                )
                binding = engine._mode_evolvers[EvolveMode.EXPERIENCE_CONSOLIDATE]
                monkeypatch.setattr(binding.evolver._appraiser, "appraise", lambda units: None)
                monkeypatch.setattr(
                    binding.evolver._extractor, "extract", lambda units, context: [unit],
                )
                await provider.add_trajectory("Deployment completed", {}, "test-trajectory")
                domain = engine._experience_domain()
                assert domain.should_write_document()
                async with asyncio.timeout(5):
                    while not domain.get(scope, [unit.id]):
                        await asyncio.sleep(0.01)
                file = Path(domain._raw_markdown().root) / DOCUMENT_PATH
                assert "verified deployment steps" in file.read_text(encoding="utf-8")
                assert (await engine.get(unit.id, scope)).id == unit.id
                assert not engine._domain_store.list(scope).items
                assert not domain.list(Scope(org="other-tenant", space=scope.space)).items
                result = json.loads(await provider.handle_tool_call(
                    "agent_memory_experience", {"query": "deployment", "category": "other"},
                ))
                assert result["results"][0]["memory"] == unit.content
                assert [ref["id"] for ref in provider.take_retrieved_refs()] == [unit.id]
                dedup = binding.evolver._dedup
                duplicate = replace(unit, id="candidate")
                assert [match.id for match, _ in dedup.recall(duplicate)] == [unit.id]
                assert not dedup.recall(replace(duplicate, scope=Scope(org="other-tenant")))
                await engine.update(
                    unit.id, scope,
                    MemoryPatch(
                        content="verified deployment steps updated", mode=UpdateMode.OVERWRITE,
                    ),
                )
                assert "verified deployment steps updated" in file.read_text(encoding="utf-8")
                shadow = domain._raw_shadow_index()
                watcher = LocalWatchdog(shadow, domain._raw_markdown().root, Scope())
                path = DOCUMENT_PATH
                shadow.delete_units(scope, [unit.id])
                assert watcher._sync_one(path, False)
                file.write_text(
                    file.read_text(encoding="utf-8").replace("verified", "revised"),
                    encoding="utf-8",
                )
                assert watcher._sync_one(path, False)
                restored = await engine.get(unit.id, scope)
                assert restored.content == "revised deployment steps updated"
                assert restored.scope == scope
                result = json.loads(await provider.handle_tool_call(
                    "agent_memory_experience", {"query": "revised", "category": "other"},
                ))
                assert result["results"][0]["memory"] == restored.content
                file.unlink()
                assert watcher._sync_one(path, True)
                assert not domain.get(scope, [unit.id])
                engine._index_for_unit(restored).build([restored])
                selector = DeleteSelector(scope=scope, unit_ids=[unit.id], mode=DeleteMode.PURGE)
                contexts = await engine.permission_contexts_for_delete(selector)
                assert [context.unit_id for context in contexts] == [unit.id]
                assert await engine.delete(selector) == [unit.id]
                assert not domain.get(scope, [unit.id])
                assert file.read_text(encoding="utf-8") == ""
            finally:
                await provider.shutdown()

        asyncio.run(run())

    def test_experience_domain_store_is_non_document(self):
        import agent_memory_provider as _amp

        cfg = _amp._agent_default_config(experience_space="shared-experience")
        stores = cfg["store_manager"]["default"]["params"]["domain_stores"]
        assert "experience" in stores, "经验数据面存在"
        exp = stores["experience"]
        assert exp["write_document"] is False, "经验数据面非文档模式（KV 真源）"
        assert exp["kv_store"] == "default", "与对话共用同一 KV 端口"
        assert stores["default"].get("write_document") is None, (
            "对话数据面不显式配 write_document（回退 globals=true 走文档分流）"
        )

    def test_experience_evolver_pins_non_document_operators(self):
        import agent_memory_provider as _amp

        cfg = _amp._agent_default_config(experience_space="shared-experience")
        params = cfg["evolver"]["experience"]["params"]
        # index_builder/dedup 必须显式指名——dep 缺省按 globals.write_document=true
        # 解析成 document/shadow（文档算子），经验条目会被写进 md 文档。
        assert params["index_builder"] == "experience"
        assert params["dedup"] == "experience"
        assert params["extractor"] == "experience"
        assert params["appraiser"] == "experience"
        # embedder 同理：匿名缺省 hashing 与查询侧 parser 的 embedder 不同源，
        # 向量召回会失真。
        assert cfg["constructor"]["experience"]["params"]["embedder"] == "default"
        assert cfg["dedup"]["experience"]["params"]["embedder"] == "default"

    def test_experience_space_renders_into_globals(self):
        import agent_memory_provider as _amp

        cfg = _amp._agent_default_config(experience_space="p_agent-memory")
        exp = cfg["globals"]["experience"]
        assert exp["default_space"] == "p_agent-memory", "经验落点随参数渲染"
        assert "curated_spaces" not in exp, "默认装配不再生成空间清单"
        assert exp["skill"] is False, "技能生成独立控制，默认关闭"

    @pytest.mark.parametrize("capabilities", [[], ["bash"], ["unrelated-tool"]])
    def test_search_experience_does_not_filter_by_capabilities(self, capabilities):
        from jiuwen_memory.common.type_def.memory import MemoryTier, MemoryUnit
        from jiuwen_memory.common.type_def.memory_filter import matches_memory_unit

        calls = []

        def search(query, context, **kwargs):
            calls.append((context, kwargs))
            return SimpleNamespace(items=[])

        def security_provider():
            return None

        client = object.__new__(_InProcessClient)
        client._api = SimpleNamespace(search=search)
        client._security_provider = security_provider
        asyncio.run(client.search_experience(
            "修复构建", ApiScope(org="acme", space="project-a"),
            category="other", capabilities=capabilities,
        ))
        context, kwargs = calls[0]
        assert context.scope.org == "acme" and context.scope.space == "project-a"
        assert context.extensions["memory_type"] == "experience"
        assert context.extensions["preferred_experience_category"] == "other"
        filters = normalize_filters(kwargs["filters"])
        assert matches_memory_unit(MemoryUnit(system_metadata={
            "memory_type": "experience", "category": "another-category",
        }), filters)
        # 走实际 SDK 客户端产生的过滤条件，再用内核真源匹配器验证。
        for tier in (MemoryTier.SEMANTIC, MemoryTier.PROCEDURAL):
            for capability in (None, "bash", "python"):
                metadata = {"memory_type": "experience", "category": "other"}
                if capability is not None:
                    metadata["capability"] = capability
                unit = MemoryUnit(tier=tier, system_metadata=metadata)
                assert matches_memory_unit(unit, filters), (capabilities, tier, capability)
        for metadata in (
            {"memory_type": "conversation", "category": "other"},
        ):
            assert not matches_memory_unit(MemoryUnit(system_metadata=metadata), filters)

    def test_experience_segments_absent_by_default(self):
        """缺省（空串）关闭——对齐方案 §2.3「配置了才启用，缺省关闭」。

        输出与无经验段的对话装配完全一致：无 pipeline 段（AgentEngine 不注入）、
        无 experience 数据面与六个具名算子、globals 无经验落点——闲置算子根本
        不装配，不只是行为面关闭。
        """
        import agent_memory_provider as _amp

        cfg = _amp._agent_default_config()
        assert "experience" not in cfg["globals"], "无经验落点"
        assert "pipeline" not in cfg, "无 pipeline 段（引擎不注入路由）"
        stores = cfg["store_manager"]["default"]["params"]["domain_stores"]
        assert set(stores) == {"default"}, "只建 default 数据面"
        for namespace in (
            "retriever", "extractor", "constructor", "dedup", "evolver", "appraiser"
        ):
            assert "experience" not in cfg.get(namespace, {}), (
                f"{namespace} 无经验算子（appraiser 整段缺省不存在）"
            )


class TestExperienceTool:
    def test_schema_registered_when_enabled(self):
        provider = _experience_provider()
        names = [s["name"] for s in provider.get_tool_schemas()]
        assert "agent_memory_experience" in names, "经验能力开启时注册查经验工具"

    def test_schema_absent_when_disabled(self):
        provider = _experience_provider(experience_space="")
        names = [s["name"] for s in provider.get_tool_schemas()]
        assert "agent_memory_experience" not in names, "空串关闭经验能力"

    def test_system_prompt_includes_category_catalog(self):
        provider = _experience_provider()
        block = provider.system_prompt_block()
        assert "agent_memory_experience" in block
        assert "software-engineering-devops" in block, "类别目录进提示词（§2.4）"
        disabled = _experience_provider(experience_space="")
        assert "agent_memory_experience" not in disabled.system_prompt_block()

    def test_tool_call_records_retrieved_refs(self):
        provider = _experience_provider()
        fake: _FakeExperienceClient = provider._client
        fake._search_items = [
            {
                "item_id": "m-0001",
                "content": "uvicorn 启动前先检查端口占用",
                "score": 0.9,
                "system_metadata": {
                    "category": "software-engineering-devops",
                    "usage_ref": "ref-abc",
                },
            },
        ]
        result = json.loads(
            asyncio.run(
                provider.handle_tool_call(
                    "agent_memory_experience",
                    {
                        "query": "启动 Python 服务",
                        "category": "software-engineering-devops",
                    },
                )
            )
        )
        assert result["count"] == 1
        assert result["results"][0]["memory"].startswith("uvicorn")
        assert "unverified" in result["source"], "标明不可信参考资料（§2.4）"
        # record_retrieved：每条 id + 引用值
        refs = provider.take_retrieved_refs()
        assert refs == [{"id": "m-0001", "usage_ref": "ref-abc"}]
        assert provider.take_retrieved_refs() == [], "取走即清空"

    def test_tool_call_scope_is_experience_space_without_user(self):
        provider = _experience_provider()
        fake: _FakeExperienceClient = provider._client
        asyncio.run(
            provider.handle_tool_call(
                "agent_memory_experience",
                {"query": "部署服务", "category": "other"},
            )
        )
        call = fake.search_calls[0]
        assert call["scope"].space == "shared-experience", "检索目标是经验空间"
        assert call["scope"].user == "", "经验条目不属于任何用户（§2.4）"
        assert call["top_k"] == 5, "固定返回条数（模型不可改）"
        assert call["capabilities"] == [], "保留客户端兼容参数；经验检索不据此过滤"

    def test_tool_call_accepts_query_without_category(self):
        provider = _experience_provider()
        payload = json.loads(asyncio.run(
            provider.handle_tool_call("agent_memory_experience", {"query": "CSV 导出"}),
        ))
        assert "error" not in payload
        assert provider._client.search_calls[0]["category"] == ""

    def test_tool_call_missing_params_returns_error(self):
        provider = _experience_provider()
        result = json.loads(
            asyncio.run(provider.handle_tool_call("agent_memory_experience", {}))
        )
        assert "error" in result, "缺 query 报参数错误"


class TestAddTrajectory:
    def test_passes_route_keys_and_session_axis(self):
        provider = _experience_provider()
        asyncio.run(provider.initialize(user_id="exp-user", scope_id="tenant-exp"))
        fake: _FakeExperienceClient = provider._client
        asyncio.run(
            provider.add_trajectory(
                '{"messages": []}',
                {"trajectory_id": "t-1", "tool_call_count": "3"},
                "session-42",
            )
        )
        content, scope, metadata = fake.add_calls[0]
        assert content == '{"messages": []}'
        assert scope.session == "session-42", "会话标识来自轨迹参数（§2.1）"
        assert scope.org == "tenant-exp" and scope.user == "exp-user"
        assert metadata["memory_type"] == "experience", "路由键并入"
        assert metadata["infer"] == "true"
        assert metadata["trajectory_id"] == "t-1", "调用方的概要字段透传"

    def test_raises_back_on_client_error(self):
        provider = _experience_provider()
        provider._client = _FakeExperienceClient(error=RuntimeError("boom"))
        # 出错抛回供发送方重发（§2.1）——不吞、不降级。
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(provider.add_trajectory("{}", {"trajectory_id": "t-2"}, "s"))


class TestSkillSync:
    def test_preserves_local_edits_and_assets_during_update_and_removal(self, tmp_path):
        provider = _experience_provider(skills_dir=str(tmp_path))
        fake = provider._client
        fake._skills = [self._skill("edited", 1), self._skill("with-assets", 1)]
        asyncio.run(provider.sync_skills())
        edited = tmp_path / "distilled" / "edited" / "SKILL.md"
        edited.write_text("user changes", encoding="utf-8")
        asset = tmp_path / "distilled" / "with-assets" / "notes.txt"
        asset.write_text("local notes", encoding="utf-8")
        fake._skills = [self._skill("edited", 2)]
        asyncio.run(provider.sync_skills())
        assert edited.read_text(encoding="utf-8") == "user changes"
        assert asset.read_text(encoding="utf-8") == "local notes"
        assert not (asset.parent / "SKILL.md").exists()
        assert not (tmp_path / "_mem2").exists()
        assert not (tmp_path / "learned").exists()

    def test_updates_owned_skill_and_preserves_unmanaged_skill_on_removal(self, tmp_path):
        provider = _experience_provider(skills_dir=str(tmp_path))
        fake = provider._client
        fake._skills = [self._skill("managed", 1)]
        asyncio.run(provider.sync_skills())
        fake._skills = [self._skill("managed", 2)]
        asyncio.run(provider.sync_skills())
        target = tmp_path / "distilled" / "managed" / "SKILL.md"
        assert "v2" in target.read_text(encoding="utf-8")
        local = tmp_path / "local" / "SKILL.md"
        local.parent.mkdir()
        local.write_text("manual skill", encoding="utf-8")
        fake._skills = []
        asyncio.run(provider.sync_skills())
        assert not target.exists()
        assert local.read_text(encoding="utf-8") == "manual skill"

    @staticmethod
    def _skill(name, version, content=None, capabilities=None):
        return {
            "item_id": f"s-{name}-{version}",
            "content": content or f"# {name} SKILL v{version}\nbody",
            "skill_name": name,
            "capabilities": capabilities or [],
            "system_metadata": {
                "skill_name": name,
                "skill_version": version,
                "capabilities": capabilities or [],
            },
        }

    def test_writes_latest_version_and_cleans_stale(self, tmp_path):
        provider = _experience_provider(
            skills_dir=str(tmp_path), experience_capabilities=["bash"]
        )
        fake: _FakeExperienceClient = provider._client
        fake._skills = [
            self._skill("start-service", 1, content="# start-service v1\nold"),
            self._skill("start-service", 2, content="# start-service v2\nnew"),
            self._skill("stale-skill", 1),
        ]
        distilled = tmp_path / "distilled"
        fake._skills = [self._skill("removed-earlier", 1)]
        asyncio.run(provider.sync_skills())
        fake._skills = [
            self._skill("start-service", 1, content="# start-service v1\nold"),
            self._skill("start-service", 2, content="# start-service v2\nnew"),
            self._skill("stale-skill", 1),
        ]
        asyncio.run(provider.sync_skills())
        written = (distilled / "start-service" / "SKILL.md").read_text(encoding="utf-8")
        assert written == "# start-service v2\nnew", "同名取版本号最大的一版"
        assert not (distilled / "removed-earlier").exists(), "清理不在结果里的目录"
        assert (distilled / "stale-skill" / "SKILL.md").exists(), "结果内的合规技能保留"

    def test_skips_invalid_local_existing_and_beyond_capability(self, tmp_path):
        provider = _experience_provider(
            skills_dir=str(tmp_path), experience_capabilities=["bash"]
        )
        fake: _FakeExperienceClient = provider._client
        (tmp_path / "local-skill").mkdir()
        fake._skills = [
            self._skill("Bad_Name!", 1),  # 不合规字符集
            self._skill("../escape", 1),  # 路径逃逸形态（同样不合规）
            self._skill("local-skill", 1),  # 本地已有同名技能
            self._skill("needs-docker", 1, capabilities=["docker"]),  # 清单外工具
            self._skill("ok-skill", 1, capabilities=["bash"]),
        ]
        asyncio.run(provider.sync_skills())
        distilled = tmp_path / "distilled"
        assert (distilled / "ok-skill" / "SKILL.md").exists(), "合规技能写入"
        assert not (distilled / "Bad_Name!").exists(), "不合规名跳过"
        assert not (distilled / "local-skill").exists(), (
            "本地已有同名技能跳过（本地优先）"
        )
        assert not (distilled / "needs-docker").exists(), "清单外工具的技能跳过"
        written_names = [p.name for p in distilled.iterdir() if p.is_dir()]
        assert "escape" not in written_names, "路径逃逸形态不落盘"

    def test_no_rewrite_when_content_unchanged(self, tmp_path):
        provider = _experience_provider(skills_dir=str(tmp_path))
        fake: _FakeExperienceClient = provider._client
        fake._skills = [self._skill("stable-skill", 3, content="# stable\nfixed")]
        asyncio.run(provider.sync_skills())
        target = tmp_path / "distilled" / "stable-skill" / "SKILL.md"
        before = target.stat().st_mtime_ns
        asyncio.run(provider.sync_skills())
        assert target.stat().st_mtime_ns == before, "内容无变化不重写（保留 mtime）"

    def test_takes_no_action_on_fetch_error(self, tmp_path):
        provider = _experience_provider(skills_dir=str(tmp_path))
        provider._client = _FakeExperienceClient(error=RuntimeError("list failed"))
        target = tmp_path / "distilled" / "keep-me" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("existing", encoding="utf-8")
        with pytest.raises(RuntimeError, match="list failed"):
            asyncio.run(provider.sync_skills())
        assert target.read_text(encoding="utf-8") == "existing", (
            "取不到时不动现有文件，下次再试（§2.5.6）"
        )

    def test_http_partial_listing_keeps_existing_skills(self, tmp_path, monkeypatch):
        provider = _experience_provider(skills_dir=str(tmp_path))
        client = _HttpClient("http://localhost")
        provider._client = client
        monkeypatch.setattr("agent_memory_provider._SKILL_PAGE_SIZE", 1)
        client._post_verb = AsyncMock(side_effect=[
            {"count": 2, "items": [{"id": "one"}]},
            None,
        ])
        target = tmp_path / "distilled" / "keep-me" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("existing", encoding="utf-8")
        asyncio.run(provider.sync_skills())
        assert target.read_text(encoding="utf-8") == "existing"
        assert [call.args[1]["offset"] for call in client._post_verb.await_args_list] == [0, 1]

    def test_http_registration_reports_missing_job_id(self):
        client = _HttpClient("http://localhost")
        client._post_verb = AsyncMock(return_value=None)
        with pytest.raises(AgentMemoryClientError, match="no job ID"):
            asyncio.run(client.register_consolidation(ApiScope(org="local"), 60))


class TestExperienceJobs:
    def test_initialize_registers_restore_and_consolidation(self):
        provider = _experience_provider()

        async def _run() -> None:
            await provider.initialize(user_id="exp-user", scope_id="tenant-exp")

        asyncio.run(_run())
        fake: _FakeExperienceClient = provider._client
        assert fake.restore_calls == 1, "恢复 dreaming 注册（§2.5.1）"
        assert len(fake.consolidation_calls) == 1, "注册周期整理"
        scope, interval = fake.consolidation_calls[0]
        assert scope.space == "shared-experience"
        assert interval == 259_200, "每三天一次"

    def test_initialize_failure_does_not_block(self):
        # restore/consolidation 抛错（多进程共用存储他人持锁的常态）——
        # initialize 不应失败，记忆主链路照常可用。
        provider = _experience_provider()
        provider._client = _FakeExperienceClient(error=RuntimeError("lock held"))

        async def _run() -> None:
            await provider.initialize(user_id="exp-user", scope_id="tenant-exp")

        asyncio.run(_run())
        assert provider.is_initialized, "失败记日志继续（§2.5.1）"

    def test_disabled_experience_skips_jobs(self):
        provider = _experience_provider(experience_space="")

        async def _run() -> None:
            await provider.initialize(user_id="exp-user", scope_id="tenant-exp")

        asyncio.run(_run())
        fake: _FakeExperienceClient = provider._client
        assert fake.restore_calls == 0
        assert fake.consolidation_calls == []


def test_experience_document_sync_from_markdown(tmp_path):
    from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
    from jiuwen_memory.storage.markdown_impl.local_markdown_store import LocalMarkdownStore
    from jiuwen_memory.storage.watchdog_impl.local_watchdog import LocalWatchdog

    scope = Scope(org="org/../one", space="support")
    unit = MemoryUnit(
        id="experience-one", scope=scope, segments=[Segment(content="First\nstep")],
        system_metadata={"memory_type": "experience", "category": "other"},
    )
    path = "memory/experience/EXPERIENCE.md"
    md = LocalMarkdownStore(root=str(tmp_path))
    md.write(scope, [unit])
    file = tmp_path / path
    assert file.is_file()

    class Shadow:
        def __init__(self):
            self.units: dict[str, MemoryUnit] = {}

        def sync_experience_document(self, scope, path, units):
            self.units = units

    shadow = Shadow()
    watcher = LocalWatchdog(markdown_root=str(tmp_path), shadow=shadow, scope=Scope())
    assert watcher._sync_one(path, False)
    assert shadow.units[unit.id].content == "First\nstep"
    file.write_text(file.read_text(encoding="utf-8").replace("First", "Revised"), encoding="utf-8")
    assert watcher._sync_one(path, False)
    restored = shadow.units[unit.id]
    assert (restored.scope, restored.content) == (scope, "Revised\nstep")
    assert restored.system_metadata["category"] == "other"
    assert watcher._sync_one(path, True)
    assert not shadow.units
