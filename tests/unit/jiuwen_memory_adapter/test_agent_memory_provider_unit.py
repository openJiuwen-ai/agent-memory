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
from datetime import UTC, datetime
from pathlib import Path

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
    AgentMemoryMemoryProvider,
    _HttpClient,
    _InProcessClient,
    _merge_prefetch_lines,
    _recall_filters,
    _tail_within_budget,
)

pytestmark = pytest.mark.unit


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
    ):
        monkeypatch.delenv(name, raising=False)
    _orig = _amp._agent_default_config

    def _offline() -> dict:
        cfg = _orig()
        cfg["globals"]["embedder_dim"] = 64
        cfg["globals"]["watch_document"] = False
        cfg["llm"]["default"] = "echo"
        cfg["extractor"].update(default="keyword", document_fact="keyword", team_turn="keyword")
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
            "system_metadata.is_credential": {"ne": "true"},
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
        assert filters["system_metadata.memory_class"] == {"ne": "team_memory"}, filters
        assert filters["system_metadata.is_credential"] == {"ne": "true"}, filters

    def test_filters_without_project_carry_no_project_predicate(self):
        assert "system_metadata.project" not in _recall_filters(""), "空 project 不应带项目谓词"

    def test_filters_with_project_normalize_to_and_clauses(self):
        # 层级、项目、凭据与排除团队类别由 API 边界统一解析为 AND。
        expr = normalize_filters(_recall_filters("proj-a"))
        assert expr.logic is FilterLogic.AND, expr
        clauses = {(c.field, c.op.value): c.value for c in expr.children}
        assert clauses == {
            ("tier", "in"): ["semantic", "core", "procedural"],
            ("system_metadata.project", "eq"): "proj-a",
            ("system_metadata.memory_class", "ne"): "team_memory",
            ("system_metadata.is_credential", "ne"): "true",
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

    def test_prefetch_excludes_credentials_from_both_profile_and_search(self):
        pg_conn = "数据库 postgresql://alice:s3cr3t@db.example.com:5432/app"
        redis_conn = "缓存 redis://:r3d1s-pass@cache.example.com:6379/0"
        openai_key = "OpenAI Key sk-proj1234567890abcdef1234"
        author_url = "团队文章索引 https://docs.example.com/articles?author=alice"
        s3_presigned = (
            "下载链接 https://bucket.s3.amazonaws.com/report.pdf"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIAEXAMPLE"
            "&X-Amz-Signature=0123456789abcdef"
        )

        async def _run() -> None:
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-cred-user"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-cred-user", scope_id="tenant-sdk")
            try:
                old_secret = "用户的邮箱口令是 super-secret-pass-9981。"
                secret_url = "同步地址 https://alice:s3cr3t@api.example.com/v1?token=xyz999"
                reset_mail = "密码重置通知发往 password-reset@example.org。"
                auth_pref = "The user prefers password authentication."
                prefs = [{"item_id": f"p{i}", "content": f"偏好条目{i}"} for i in range(5)]

                async def _read(md_filename, scope, *, project="", line_range=None):
                    return [
                        {"item_id": "cred1", "content": old_secret, "is_credential": True},
                        {"item_id": "cred2", "content": secret_url, "is_credential": True},
                        {"item_id": "cred3", "content": s3_presigned, "is_credential": True},
                        {"item_id": "cred4", "content": pg_conn, "is_credential": True},
                        {"item_id": "cred5", "content": redis_conn, "is_credential": True},
                        {"item_id": "cred6", "content": openai_key, "is_credential": True},
                        {"item_id": "mail1", "content": reset_mail, "is_credential": False},
                        {"item_id": "pref_auth", "content": auth_pref, "is_credential": False},
                        {"item_id": "doc1", "content": author_url, "is_credential": False},
                        *prefs,
                    ]

                seen_filters: list[dict | None] = []

                async def _search(query, scope, *, top_k=5, filters=None):
                    seen_filters.append(filters)
                    # 模拟两路同时命中带 is_credential 标记的凭据与正常条目
                    return [
                        {"item_id": "cred1", "content": old_secret, "is_credential": True},
                        {"item_id": "cred2", "content": secret_url, "is_credential": True},
                        {"item_id": "cred3", "content": s3_presigned, "is_credential": True},
                        {"item_id": "cred4", "content": pg_conn, "is_credential": True},
                        {"item_id": "cred5", "content": redis_conn, "is_credential": True},
                        {"item_id": "cred6", "content": openai_key, "is_credential": True},
                        {"item_id": "pref_auth", "content": auth_pref, "is_credential": False},
                        {"item_id": "doc1", "content": author_url, "is_credential": False},
                        {"item_id": "hit1", "content": "Calendar Demo 的通知时间是每天 09:00。"},
                    ]

                provider._client.read_md_file = _read
                provider._client.search = _search
                text = await provider.prefetch("Calendar Demo 的通知时间是什么？")
                assert seen_filters and seen_filters[0]["system_metadata.is_credential"] == {
                    "ne": "true"
                }, f"Work 预取检索应携带凭据排除过滤: {seen_filters}"
                assert "super-secret-pass-9981" not in text, (
                    "USER.md 与 search 两路命中同一凭据时均不得进入通用预取"
                )
                assert "s3cr3t" not in text and "token=xyz999" not in text, (
                    "标记为凭据的 URL 不得进入通用预取"
                )
                assert "r3d1s-pass" not in text, "标记为凭据的 Redis 连接串不得进入通用预取"
                assert "sk-proj1234567890abcdef1234" not in text, "标记为凭据的 Key 不得进入预取"
                assert "X-Amz-Signature" not in text, "标记为凭据的预签名链接不得进入通用预取"
                assert reset_mail in text, "含 password 子串的合法邮箱不应被误删"
                assert auth_pref in text, "普通英文登录偏好不应被误删"
                assert author_url in text, "带 ?author=alice 的普通链接不应被误删"
                assert "Calendar Demo 的通知时间是每天 09:00。" in text
            finally:
                await provider.shutdown()

        asyncio.run(_run())

    def test_work_system_prompt_guides_reusable_identifier_search_and_confirmation(self):
        normal = AgentMemoryMemoryProvider(
            security_provider=_security_provider(
                ApiScope(org="tenant-sdk", user="sdk-prompt-user"), role=Role.ROOT
            ),
        )
        prompt = normal.system_prompt_block()

        assert "Reusable identifiers:" in prompt
        assert "agent_memory_search" in prompt
        assert "server/endpoint address" in prompt
        assert "Use a unique match directly when it fits the task" in prompt
        assert "ask the user to confirm before acting" in prompt
        assert "never raw secrets" in prompt

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
         "compact", "compact-json", "compact-yaml"],
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
        elif configuration == "no-pipeline":
            del config["pipeline"]
        elif configuration == "wrong-evolver":
            config["pipeline"]["default"]["params"]["profiles"]["video"]["evolver"] = "default"

        async def run():
            provider = AgentMemoryMemoryProvider(
                config_dict=config,
                config_path=config_path,
                security_provider=_security_provider(ApiScope(org="tenant", user="user")),
            )
            enabled = configuration not in {"no-pipeline", "wrong-evolver"}
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

    def test_cross_project_credential_searchable_across_projects_not_prefetched(
        self, monkeypatch, tmp_path,
    ):
        import agent_memory_provider as adapter

        offline_config = adapter._agent_default_config

        def config():
            result = offline_config()
            result["extractor"]["document_fact"] = {"target": "document_fact"}
            return result

        cred_fact = "用户的邮箱 SMTP 授权码是 smtp-code-7788。"

        def extract(_self, messages):
            source_id = re.search(r"\[ID: ([^\]]+)\]", messages[1].content).group(1)
            return json.dumps([
                {
                    "source_id": source_id,
                    "content": cred_fact,
                    "memory_class": "long_term_memory",
                    "project_fact": False,
                    "is_credential": True,
                    "tier": "semantic",
                    "confidence": 1.0,
                }
            ])

        monkeypatch.setattr(adapter, "_agent_default_config", config)
        monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", extract)

        async def run():
            provider = AgentMemoryMemoryProvider(
                security_provider=_security_provider(
                    ApiScope(org="tenant-sdk", user="sdk-cred-xproj"), role=Role.ROOT
                ),
            )
            await provider.initialize(user_id="sdk-cred-xproj", scope_id="tenant-sdk")
            try:
                await provider.sync_turn("授权码是 smtp-code-7788", "记下了。", project="repo-a")
                assert (tmp_path / "memory" / "MEMORY.md").is_file(), (
                    "跨项目凭据应落入跨项目 memory/MEMORY.md"
                )
                assert not (tmp_path / "memory" / "repo-a" / "MEMORY.md").exists(), (
                    "跨项目凭据不得绑定到首次写入的项目目录"
                )
                # 省略 project 可检索跨项目凭据；传具体 project 时按等值隔离不混入空项目条目
                res = json.loads(
                    await provider.handle_tool_call(
                        "agent_memory_search", {"query": "SMTP 授权码"},
                    )
                )
                joined = "\n".join(it["memory"] for it in res.get("results", []))
                assert cred_fact in joined, "省略 project 的显式搜索应能查到跨项目凭据"
                for proj in ("repo-a", "repo-b"):
                    scoped = json.loads(
                        await provider.handle_tool_call(
                            "agent_memory_search", {"query": "SMTP 授权码", "project": proj},
                        )
                    )
                    assert all(
                        cred_fact not in it["memory"] for it in scoped.get("results", [])
                    ), f"传 project={proj!r} 时按等值过滤不应混入空项目跨项目凭据"
                assert "smtp-code-7788" not in await provider.prefetch(
                    "SMTP 授权码", project="repo-b"
                )
                # 真实进程内 search() 也必须透传 system_metadata.is_credential
                raw_hits = await provider._client.search(
                    "SMTP 授权码", provider.bound_scope(), top_k=5,
                )
                assert any(
                    cred_fact in hit["content"] and hit["is_credential"] is True
                    for hit in raw_hits
                ), f"真实 _InProcessClient.search 应透传 is_credential=True: {raw_hits}"
            finally:
                await provider.shutdown()

        asyncio.run(run())

    def test_two_session_first_time_identifier_recall_and_purpose_boundary(
        self, monkeypatch, tmp_path,
    ):
        """#313 双会话验收：首轮测试邮箱落画像，次轮同用途找回、换用途不作默认收件人。"""
        import agent_memory_provider as adapter

        offline_config = adapter._agent_default_config

        def config():
            result = offline_config()
            result["extractor"]["document_fact"] = {"target": "document_fact"}
            return result

        profile_fact = (
            "用户为这次测试文件发送指定了发件邮箱 sender@example.com "
            "和收件邮箱 recipient@example.org。"
        )
        event_fact = (
            "2026-11-21，用户要求助手用 sender@example.com 把 /tmp/test.txt "
            "发到 recipient@example.org。"
        )

        def extract(_self, messages):
            source_id = re.search(r"\[ID: ([^\]]+)\]", messages[1].content).group(1)
            return json.dumps([
                {
                    "source_id": source_id,
                    "target": "event",
                    "content": event_fact,
                    "memory_class": "daily_memory",
                    "project_fact": False,
                    "is_credential": False,
                    "tier": "episodic",
                    "confidence": 1.0,
                },
                {
                    "source_id": source_id,
                    "target": "fact",
                    "content": profile_fact,
                    "memory_class": "user_profile",
                    "project_fact": False,
                    "is_credential": False,
                    "tier": "semantic",
                    "confidence": 1.0,
                },
            ])

        monkeypatch.setattr(adapter, "_agent_default_config", config)
        monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", extract)

        async def run():
            sec = _security_provider(
                ApiScope(org="tenant-sdk", user="sdk-two-session"), role=Role.ROOT
            )
            # 会话 1：首次给出测试文件发送的发件与收件邮箱
            session1 = AgentMemoryMemoryProvider(security_provider=sec)
            await session1.initialize(user_id="sdk-two-session", scope_id="tenant-sdk")
            try:
                await session1.sync_turn(
                    "我生成了/tmp/test.txt这个文档，"
                    "用sender@example.com 发到 recipient@example.org",
                    "好的，我来准备发送。",
                )
            finally:
                await session1.shutdown()

            user_md = (tmp_path / "USER.md").read_text(encoding="utf-8")
            assert "sender@example.com" in user_md and "recipient@example.org" in user_md
            assert "/tmp/test.txt" not in user_md, "临时文件名不应写进 USER.md 稳定事实"
            assert "测试文件" in user_md and "默认收件人" not in user_md

            # 会话 2：全新会话实例，同一用户
            session2 = AgentMemoryMemoryProvider(security_provider=sec)
            await session2.initialize(user_id="sdk-two-session", scope_id="tenant-sdk")
            try:
                # 继续同一用途：开场预取与显式搜索均可直接找回发件与收件邮箱
                same_purpose = await session2.prefetch("再帮我发一次测试文件")
                assert "sender@example.com" in same_purpose
                assert "recipient@example.org" in same_purpose
                assert "测试文件" in same_purpose

                search_res = json.loads(
                    await session2.handle_tool_call(
                        "agent_memory_search", {"query": "发件邮箱 收件邮箱"},
                    )
                )
                joined = "\n".join(it["memory"] for it in search_res.get("results", []))
                assert "sender@example.com" in joined and "recipient@example.org" in joined

                # 换用途（如发送周报）：画像保留“测试文件”用途，提示词约束用途不符时须先确认
                other_purpose = await session2.prefetch("把本周周报发出去")
                assert "测试文件" in other_purpose and "默认收件人" not in other_purpose
                prompt = session2.system_prompt_block()
                assert "stored purpose does not match" in prompt
                assert "ask the user to confirm before acting" in prompt
            finally:
                await session2.shutdown()

        asyncio.run(run())

    def test_dual_session_email_and_credential_written_twice_and_unrelated_prefetch(
        self, tmp_path, monkeypatch,
    ):
        """#313 双会话验收：邮箱与授权码写两次，换文件准备发送可找回，无关会话不预取。"""
        import agent_memory_provider as adapter

        offline_config = adapter._agent_default_config

        def config():
            result = offline_config()
            result["extractor"]["document_fact"] = {"target": "document_fact"}
            return result

        email_fact = (
            "用户为发送测试文件指定了发件邮箱 sender@example.com "
            "和收件邮箱 recipient@example.org。"
        )
        cred_fact = (
            "用户发件邮箱 sender@example.com 的 QQ 邮箱 SMTP 授权码是 smtp-auth-e2e-9876。"
        )

        def extract(_self, messages):
            source_id = re.search(r"\[ID: ([^\]]+)\]", messages[1].content).group(1)
            return json.dumps([
                {
                    "source_id": source_id,
                    "target": "fact",
                    "content": email_fact,
                    "memory_class": "user_profile",
                    "project_fact": False,
                    "is_credential": False,
                    "tier": "semantic",
                    "confidence": 1.0,
                },
                {
                    # 字符串 "true" 且误归 user_profile，验证抽取入口统一收口为凭据长期记忆
                    "source_id": source_id,
                    "target": "fact",
                    "content": cred_fact,
                    "memory_class": "user_profile",
                    "project_fact": False,
                    "is_credential": "true",
                    "tier": "semantic",
                    "confidence": 1.0,
                },
            ])

        monkeypatch.setattr(adapter, "_agent_default_config", config)
        monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", extract)

        async def run():
            sec = _security_provider(
                ApiScope(org="tenant-sdk", user="sdk-e2e-email"), role=Role.ROOT
            )
            # 会话 A：#313 原话 + 测试凭据，同一授权码写两次验证不丢标记、不重复
            s1 = AgentMemoryMemoryProvider(security_provider=sec)
            await s1.initialize(user_id="sdk-e2e-email", scope_id="tenant-sdk")
            try:
                for _ in range(2):
                    await s1.sync_turn(
                        "我生成了/tmp/test.txt这个文档，用sender@example.com 发到 "
                        "recipient@example.org，QQ 邮箱授权码是 smtp-auth-e2e-9876",
                        "好的，已准备好。",
                    )
            finally:
                await s1.shutdown()

            user_md = (tmp_path / "USER.md").read_text(encoding="utf-8")
            memory_md = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
            assert "sender@example.com" in user_md and "recipient@example.org" in user_md
            assert "smtp-auth-e2e-9876" not in user_md, "授权码不得写入 USER.md"
            assert memory_md.count("smtp-auth-e2e-9876") == 1, "同一授权码写两次应只保留一条"

            # 会话 B：换一份文件、只准备发送
            s2 = AgentMemoryMemoryProvider(security_provider=sec)
            await s2.initialize(user_id="sdk-e2e-email", scope_id="tenant-sdk")
            try:
                prefetched = await s2.prefetch("帮我准备把 /tmp/report.pdf 发出去")
                assert "sender@example.com" in prefetched and "recipient@example.org" in prefetched
                assert "发送测试文件" in prefetched
                assert "smtp-auth-e2e-9876" not in prefetched, "开场预取不得注入授权码"

                found = json.loads(
                    await s2.handle_tool_call(
                        "agent_memory_search",
                        {"query": "sender@example.com QQ 邮箱 SMTP 授权码"},
                    )
                )
                cred_hits = [
                    it for it in found.get("results", []) if "smtp-auth-e2e-9876" in it["memory"]
                ]
                assert len(cred_hits) == 1, "授权码应可按对应账号/服务名显式找回且仅一条"

                # 无关会话：预取不得包含授权码
                unrelated = await s2.prefetch("今天北京天气怎么样")
                assert "smtp-auth-e2e-9876" not in unrelated
            finally:
                await s2.shutdown()

        asyncio.run(run())

    def test_dual_session_service_account_endpoint_and_credential_recall(
        self, tmp_path, monkeypatch,
    ):
        """通用标识双会话验收：服务账号 + 接口地址 + 关联凭据共用同一写入与按需检索规则。"""
        import agent_memory_provider as adapter

        offline_config = adapter._agent_default_config

        def config():
            result = offline_config()
            result["extractor"]["document_fact"] = {"target": "document_fact"}
            return result

        svc_fact = (
            "用户推送容器镜像使用服务账号 svc_deploy "
            "和镜像仓库接口地址 https://registry.internal.example.com。"
        )
        token_fact = (
            "用户服务账号 svc_deploy 访问 https://registry.internal.example.com "
            "的仓库令牌是 tok-reg-5566。"
        )

        def extract(_self, messages):
            source_id = re.search(r"\[ID: ([^\]]+)\]", messages[1].content).group(1)
            return json.dumps([
                {
                    "source_id": source_id,
                    "target": "fact",
                    "content": svc_fact,
                    "memory_class": "user_profile",
                    "project_fact": False,
                    "is_credential": False,
                    "tier": "semantic",
                    "confidence": 1.0,
                },
                {
                    "source_id": source_id,
                    "target": "fact",
                    "content": token_fact,
                    "memory_class": "long_term_memory",
                    "project_fact": False,
                    "is_credential": True,
                    "tier": "semantic",
                    "confidence": 1.0,
                },
            ])

        monkeypatch.setattr(adapter, "_agent_default_config", config)
        monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", extract)

        async def run():
            sec = _security_provider(
                ApiScope(org="tenant-sdk", user="sdk-e2e-svc"), role=Role.ROOT
            )
            s1 = AgentMemoryMemoryProvider(security_provider=sec)
            await s1.initialize(user_id="sdk-e2e-svc", scope_id="tenant-sdk")
            try:
                await s1.sync_turn(
                    "发布镜像时用账号 svc_deploy 推送到 https://registry.internal.example.com，"
                    "仓库令牌是 tok-reg-5566",
                    "好的，准备推送。",
                )
            finally:
                await s1.shutdown()

            user_md = (tmp_path / "USER.md").read_text(encoding="utf-8")
            assert "svc_deploy" in user_md
            assert "https://registry.internal.example.com" in user_md
            assert "tok-reg-5566" not in user_md

            s2 = AgentMemoryMemoryProvider(security_provider=sec)
            await s2.initialize(user_id="sdk-e2e-svc", scope_id="tenant-sdk")
            try:
                prefetched = await s2.prefetch("帮我把新镜像推到仓库")
                assert "svc_deploy" in prefetched
                assert "https://registry.internal.example.com" in prefetched
                assert "tok-reg-5566" not in prefetched

                search_svc = json.loads(
                    await s2.handle_tool_call(
                        "agent_memory_search", {"query": "镜像仓库 服务账号 接口地址"},
                    )
                )
                joined_svc = "\n".join(it["memory"] for it in search_svc.get("results", []))
                assert "svc_deploy" in joined_svc
                assert "https://registry.internal.example.com" in joined_svc

                search_tok = json.loads(
                    await s2.handle_tool_call(
                        "agent_memory_search", {"query": "svc_deploy 仓库令牌"},
                    )
                )
                assert any("tok-reg-5566" in it["memory"] for it in search_tok.get("results", []))
            finally:
                await s2.shutdown()

        asyncio.run(run())

    def test_conclude_project_credential_stays_isolated_from_other_projects(
        self, tmp_path, monkeypatch,
    ):
        """agent_memory_conclude 显式传 project 写凭据时，即使 project_fact=false 也留在该项目。"""
        import agent_memory_provider as adapter

        offline_config = adapter._agent_default_config

        def config():
            result = offline_config()
            result["extractor"]["document_fact"] = {"target": "document_fact"}
            return result

        cred_fact = "项目 A 的部署令牌是 tok-proj-a-only。"

        def extract(_self, messages):
            source_id = re.search(r"\[ID: ([^\]]+)\]", messages[1].content).group(1)
            return json.dumps([
                {
                    "source_id": source_id,
                    "target": "fact",
                    "content": cred_fact,
                    "memory_class": "long_term_memory",
                    "project_fact": False,
                    "is_credential": True,
                    "tier": "semantic",
                    "confidence": 1.0,
                }
            ])

        monkeypatch.setattr(adapter, "_agent_default_config", config)
        monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", extract)

        async def run():
            sec = _security_provider(
                ApiScope(org="tenant-sdk", user="sdk-conclude-proj"), role=Role.ROOT
            )
            p = AgentMemoryMemoryProvider(security_provider=sec)
            await p.initialize(user_id="sdk-conclude-proj", scope_id="tenant-sdk")
            try:
                await p.handle_tool_call(
                    "agent_memory_conclude",
                    {"conclusion": cred_fact, "project": "proj-a"},
                )
                assert cred_fact in (tmp_path / "memory" / "proj-a" / "MEMORY.md").read_text(
                    encoding="utf-8"
                )
                assert not (tmp_path / "memory" / "MEMORY.md").exists()

                in_b = json.loads(
                    await p.handle_tool_call(
                        "agent_memory_search",
                        {"query": "tok-proj-a-only", "project": "proj-b"},
                    )
                )
                assert all(
                    "tok-proj-a-only" not in it["memory"] for it in in_b.get("results", [])
                ), "写入 proj-a 的凭据不得在 proj-b 被搜到"
                in_a = json.loads(
                    await p.handle_tool_call(
                        "agent_memory_search",
                        {"query": "tok-proj-a-only", "project": "proj-a"},
                    )
                )
                assert any("tok-proj-a-only" in it["memory"] for it in in_a.get("results", []))
                pref = await p.prefetch("tok-proj-a-only", project="proj-a")
                assert "tok-proj-a-only" not in pref
            finally:
                await p.shutdown()

        asyncio.run(run())

    def test_project_email_and_subsequent_global_profile_coexist(
        self, tmp_path, monkeypatch,
    ):
        """项目邮箱仅落该项目，且已有项目邮箱时后续同项目对话写全局 user_profile 不被判重跳过。"""
        import agent_memory_provider as adapter

        offline_config = adapter._agent_default_config

        def config():
            result = offline_config()
            result["extractor"]["document_fact"] = {"target": "document_fact"}
            return result

        email_fact = "项目 A 的测试收件邮箱是 receiver-a@example.com。"
        shared_email = "通用收件邮箱是 shared-ops@example.com。"
        weekly_email = "周报收件邮箱是 weekly-report@example.com。"

        def extract(_self, messages):
            match = re.search(r"\[ID: ([^\]]+)\]\s*(.*)", messages[1].content)
            assert match is not None
            source_id, line = match.group(1), match.group(2)
            if "全局默认" in line or "各项目都用这个邮箱" in line:
                fact = shared_email if "shared-ops@example.com" in line else weekly_email
                return json.dumps([
                    {
                        "source_id": source_id,
                        "target": "fact",
                        "content": fact,
                        "memory_class": "user_profile",
                        "project_fact": False,
                        "is_credential": False,
                        "tier": "semantic",
                        "confidence": 1.0,
                    }
                ])
            if "shared-ops@example.com" in line:
                fact = shared_email
            elif "weekly-report@example.com" in line:
                fact = weekly_email
            else:
                fact = email_fact
            return json.dumps([
                {
                    "source_id": source_id,
                    "target": "fact",
                    "content": fact,
                    "memory_class": "long_term_memory",
                    "project_fact": True,
                    "is_credential": False,
                    "tier": "semantic",
                    "confidence": 1.0,
                }
            ])

        monkeypatch.setattr(adapter, "_agent_default_config", config)
        monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", extract)

        async def run():
            sec = _security_provider(
                ApiScope(org="tenant-sdk", user="sdk-conclude-email-proj"), role=Role.ROOT
            )
            p = AgentMemoryMemoryProvider(security_provider=sec)
            await p.initialize(user_id="sdk-conclude-email-proj", scope_id="tenant-sdk")
            try:
                await p.handle_tool_call(
                    "agent_memory_conclude",
                    {"conclusion": email_fact, "project": "proj-a"},
                )
                assert email_fact in (tmp_path / "memory" / "proj-a" / "MEMORY.md").read_text(
                    encoding="utf-8"
                )
                assert not (tmp_path / "USER.md").exists(), (
                    "项目专用邮箱（project_fact=true）不得落进全局 USER.md"
                )

                in_b = json.loads(
                    await p.handle_tool_call(
                        "agent_memory_search",
                        {"query": "receiver-a@example.com", "project": "proj-b"},
                    )
                )
                assert all(
                    "receiver-a@example.com" not in it["memory"] for it in in_b.get("results", [])
                ), "项目 B 搜索不得读到项目 A 的邮箱"
                pref_b = await p.prefetch("receiver-a@example.com", project="proj-b")
                assert "receiver-a@example.com" not in pref_b, (
                    "项目 B 预取不得带出项目 A 的邮箱"
                )

                in_a = json.loads(
                    await p.handle_tool_call(
                        "agent_memory_search",
                        {"query": "receiver-a@example.com", "project": "proj-a"},
                    )
                )
                assert any(
                    "receiver-a@example.com" in it["memory"] for it in in_a.get("results", [])
                )
                pref_a = await p.prefetch("receiver-a@example.com", project="proj-a")
                assert "receiver-a@example.com" in pref_a

                # 先全局后项目：全局已有同文邮箱时，明确归属项目 A（project_fact=true）
                # 不被全局同名记录判重跳过，且项目内连写两次去重保留 1 条、按项目可搜回
                await p.handle_tool_call(
                    "agent_memory_conclude", {"conclusion": f"全局默认：{shared_email}"}
                )
                assert shared_email in (tmp_path / "USER.md").read_text(encoding="utf-8")
                for _ in range(2):
                    await p.handle_tool_call(
                        "agent_memory_conclude",
                        {"conclusion": shared_email, "project": "proj-a"},
                    )
                proj_a_md = (tmp_path / "memory" / "proj-a" / "MEMORY.md").read_text(
                    encoding="utf-8"
                )
                assert proj_a_md.count("shared-ops@example.com") == 1, (
                    "全局已有同文邮箱时，明确写入项目 A 不得被全局判重跳过，同项目连写应去重留 1 条"
                )
                assert shared_email in (tmp_path / "USER.md").read_text(encoding="utf-8")
                in_a_shared = json.loads(
                    await p.handle_tool_call(
                        "agent_memory_search",
                        {"query": "shared-ops@example.com", "project": "proj-a"},
                    )
                )
                assert any(
                    "shared-ops@example.com" in it["memory"]
                    for it in in_a_shared.get("results", [])
                ), "指定项目 A 搜索应能找回后写入项目 A 的同文邮箱"

                # 先项目后全局：先存项目 A 邮箱，再在项目 A 普通对话中声明跨项目复用（user_profile）
                await p.handle_tool_call(
                    "agent_memory_conclude",
                    {"conclusion": weekly_email, "project": "proj-a"},
                )
                assert weekly_email in (tmp_path / "memory" / "proj-a" / "MEMORY.md").read_text(
                    encoding="utf-8"
                )
                await p.sync_turn(
                    f"各项目都用这个邮箱：{weekly_email}", "好的，已记住", project="proj-a"
                )
                # 再说一次验证全局 USER.md 内部正常去重只留 1 条
                await p.sync_turn(
                    f"各项目都用这个邮箱：{weekly_email}", "好的", project="proj-a"
                )
                user_md = (tmp_path / "USER.md").read_text(encoding="utf-8")
                assert user_md.count("weekly-report@example.com") == 1, (
                    "已有项目邮箱不得让后续全局 user_profile 漏存，全局 USER.md 应保留 1 条"
                )
                global_weekly = json.loads(
                    await p.handle_tool_call(
                        "agent_memory_search",
                        {"query": "weekly-report@example.com"},
                    )
                )
                assert any(
                    "weekly-report@example.com" in it["memory"]
                    for it in global_weekly.get("results", [])
                ), "省略 project 的全局搜索应能找回后续写入 USER.md 的邮箱"
                pref_b_weekly = await p.prefetch("发周报", project="proj-b")
                assert "weekly-report@example.com" in pref_b_weekly, (
                    "项目 B 预取应能带出后续写入全局 USER.md 的邮箱"
                )
            finally:
                await p.shutdown()

        asyncio.run(run())

    def test_project_credential_dedup_does_not_merge_into_global_record(
        self, tmp_path, monkeypatch,
    ):
        """项目 A 凭据不并入全局泄露到项目 B，且全局已有同文凭据时项目 A 连写三次只保留 1 条。"""
        import agent_memory_provider as adapter

        offline_config = adapter._agent_default_config

        def config():
            result = offline_config()
            result["extractor"]["document_fact"] = {"target": "document_fact"}
            return result

        global_fact = "通用服务账号 svc_api 的默认 API Token 是 tok-shared-text。"
        proj_a_only_fact = "项目 A 服务账号 svc_api 的专用 API Token 是 tok-proj-a-secret。"
        phase = {"step": 0}

        def extract(_self, messages):
            source_id = re.search(r"\[ID: ([^\]]+)\]", messages[1].content).group(1)
            phase["step"] += 1
            if phase["step"] <= 6:
                return json.dumps([
                    {
                        "source_id": source_id,
                        "target": "fact",
                        "content": f"{global_fact}（全局条目 {phase['step']}）",
                        "memory_class": "long_term_memory",
                        "project_fact": False,
                        "is_credential": True,
                        "tier": "semantic",
                        "confidence": 1.0,
                    }
                ])
            if phase["step"] == 7:
                return json.dumps([
                    {
                        "source_id": source_id,
                        "target": "fact",
                        "content": proj_a_only_fact,
                        "memory_class": "long_term_memory",
                        "project_fact": True,
                        "is_credential": True,
                        "tier": "semantic",
                        "confidence": 1.0,
                    }
                ])
            return json.dumps([
                {
                    "source_id": source_id,
                    "target": "fact",
                    "content": global_fact,
                    "memory_class": "long_term_memory",
                    "project_fact": True,
                    "is_credential": True,
                    "tier": "semantic",
                    "confidence": 1.0,
                }
            ])

        monkeypatch.setattr(adapter, "_agent_default_config", config)
        monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", extract)

        async def run():
            sec = _security_provider(
                ApiScope(org="tenant-sdk", user="sdk-proj-cred-dedup"), role=Role.ROOT
            )
            p = AgentMemoryMemoryProvider(security_provider=sec)
            await p.initialize(user_id="sdk-proj-cred-dedup", scope_id="tenant-sdk")
            try:
                # 先写入 6 条全局高相似凭据（超过 dedup top_k=5），验证真实 ShadowDedup 召回不被占满
                for idx in range(1, 7):
                    await p.handle_tool_call(
                        "agent_memory_conclude",
                        {"conclusion": f"{global_fact}（全局条目 {idx}）"},
                    )

                # 写入项目 A 专用凭据：不得并入全局记录泄露到项目 B
                await p.handle_tool_call(
                    "agent_memory_conclude",
                    {"conclusion": proj_a_only_fact, "project": "proj-a"},
                )
                # 全局已有 6 条高相似凭据时，向项目 A 连续写入同文凭据三次：项目 A 只保留 1 条
                for _ in range(3):
                    await p.handle_tool_call(
                        "agent_memory_conclude",
                        {"conclusion": global_fact, "project": "proj-a"},
                    )

                proj_a_md = (tmp_path / "memory" / "proj-a" / "MEMORY.md").read_text(
                    encoding="utf-8"
                )
                global_md = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
                assert proj_a_md.count("tok-shared-text") == 1, (
                    "全局有 6 条高相似凭据占满 top_k=5 时，项目 A 连写三次仍须去重只保留 1 条"
                )
                assert global_md.count("tok-shared-text") == 6
                assert "tok-proj-a-secret" in proj_a_md
                assert "tok-proj-a-secret" not in global_md

                in_b = json.loads(
                    await p.handle_tool_call(
                        "agent_memory_search",
                        {"query": "tok-proj-a-secret", "project": "proj-b"},
                    )
                )
                assert all(
                    "tok-proj-a-secret" not in it["memory"] for it in in_b.get("results", [])
                ), "项目 A 凭据不得因去重并入全局记录而泄露到项目 B"

                in_a = json.loads(
                    await p.handle_tool_call(
                        "agent_memory_search",
                        {"query": "tok-proj-a-secret", "project": "proj-a"},
                    )
                )
                assert any(
                    "tok-proj-a-secret" in it["memory"] for it in in_a.get("results", [])
                ), "项目 A 内应能正常检索到项目 A 专用凭据"
                in_a_global = json.loads(
                    await p.handle_tool_call(
                        "agent_memory_search",
                        {"query": "tok-shared-text", "project": "proj-a"},
                    )
                )
                assert any(
                    "tok-shared-text" in it["memory"] for it in in_a_global.get("results", [])
                ), "全局与项目 A 凭据应均保持可检索"
            finally:
                await p.shutdown()

        asyncio.run(run())

    def test_http_client_search_propagates_is_credential(self):
        from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import (
            _HttpClient,
            _Scope,
        )

        client = _HttpClient(base_url="http://127.0.0.1:9999", api_key="test")

        async def _fake_post(verb, payload):
            assert verb == "search"
            return {
                "items": [
                    {
                        "unit_id": "u-cred",
                        "content": "SMTP 授权码是 xxx",
                        "score": 0.9,
                        "system_metadata": {"is_credential": "true"},
                    },
                    {
                        "unit_id": "u-ok",
                        "content": "发件邮箱是 a@example.com",
                        "score": 0.8,
                        "system_metadata": {},
                    },
                ]
            }

        client._post_verb = _fake_post
        hits = asyncio.run(client.search("SMTP", _Scope(org="o", user="u")))
        assert hits == [
            {
                "item_id": "u-cred",
                "content": "SMTP 授权码是 xxx",
                "score": 0.9,
                "is_credential": True,
            },
            {
                "item_id": "u-ok",
                "content": "发件邮箱是 a@example.com",
                "score": 0.8,
                "is_credential": False,
            },
        ]

    def test_in_process_add_logs_only_exception_type(self, caplog):
        from jiuwen_memory.construction.extractor_impl.llm_extractor import (
            InvalidExtractionCandidateError,
        )
        from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import (
            _InProcessClient,
            _Scope,
        )
        from tests.unit.construction.fixtures import MockLLM, create_test_unit

        class _BoomApi:
            def __init__(self, exc: Exception) -> None:
                self._exc = exc

            async def add_async(self, *_args, **_kwargs):
                raise self._exc

        def _no_security():
            return None

        scope = _Scope(org="o", user="u")
        client = object.__new__(_InProcessClient)
        client._security = _no_security

        extractor = AgentLlmExtractor(llm=MockLLM())
        with pytest.raises(InvalidExtractionCandidateError) as invalid:
            extractor.build_candidates(
                [{"source_id": "sender@example.org", "confidence": 1}],
                [create_test_unit("u1", "测试邮件")],
            )
        assert str(invalid.value) == "item 0 source_id is not in the batch"
        client._api = _BoomApi(invalid.value)
        with caplog.at_level("WARNING"):
            assert asyncio.run(client.add("msg", scope)) is None
        assert "error_type=InvalidExtractionCandidateError" in caplog.text
        assert "source_id" not in caplog.text
        assert "sender@example.org" not in caplog.text

        caplog.clear()
        client._api = _BoomApi(RuntimeError("secret-plaintext-never-log"))
        with caplog.at_level("WARNING"):
            assert asyncio.run(client.add("msg", scope)) is None
        assert "error_type=RuntimeError" in caplog.text
        assert "secret-plaintext-never-log" not in caplog.text

        caplog.clear()
        client._api = _BoomApi(InvalidExtractionCandidateError("sender@example.org"))
        with caplog.at_level("WARNING"):
            assert asyncio.run(client.add("msg", scope)) is None
        assert "error_type=InvalidExtractionCandidateError" in caplog.text
        assert "sender@example.org" not in caplog.text


# --------------------------------------------------------------------------- #
# _merge_config_over_default：上层 config_dict 深合并到内置 agent 默认装配
# --------------------------------------------------------------------------- #


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

        monkeypatch.setattr(_amp, "_agent_default_config", lambda: {
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

        monkeypatch.setattr(_amp, "_agent_default_config", lambda: {"globals": {"a": 1}})
        first = _amp._merge_config_over_default({"globals": {"b": 2}})
        second = _amp._merge_config_over_default({"globals": {"c": 3}})
        assert first is not second, "每次返回全新字面量"
        assert first["globals"] == {"a": 1, "b": 2}, "首次合并结果"
        assert second["globals"] == {"a": 1, "c": 3}, "无跨调用可变状态污染"


def test_source_metadata_added_to_each_provider_write_path(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

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
