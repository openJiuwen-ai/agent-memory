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
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jiuwen_memory.api import Scope as ApiScope
from jiuwen_memory.api import Surface
from jiuwen_memory.common.security import new_request_context
from jiuwen_memory.common.security.types import AuthContext, Role

# 本文件 -> tests/unit/jiuwen_memory_adapter -> tests/unit -> tests -> 仓库根
# append（而非 insert(0)）：避免模块重名告警；adapter 作为顶层模块 agent_memory_provider
# 被加载，仓库内无同名包冲突，尾部追加即命中（参照 test_jiuwenswarm_access.py）。
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADAPTER_DIR = _REPO_ROOT / "jiuwen_memory_adapter" / "jiuwenswarm"
if _ADAPTER_DIR.is_dir() and str(_ADAPTER_DIR) not in sys.path:
    sys.path.append(str(_ADAPTER_DIR))

# ruff: noqa: E402
# pylint: disable=wrong-import-order  # 顶层模块须晚于 sys.path.append，无法按 first/third-party 排序
from agent_memory_provider import AgentMemoryMemoryProvider, _HttpClient, _recall_filters

pytestmark = pytest.mark.unit


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

    _orig = _amp._agent_default_config

    def _offline(*, experience_space: str = "") -> dict:
        cfg = _orig(experience_space=experience_space)
        cfg["globals"]["embedder_dim"] = 64
        cfg["globals"]["watch_document"] = False
        # 就地改具名键（不整体替换命名空间）——mem2.0 起默认装配按 experience_space
        # 条件带经验段（extractor/appraiser/evolver/constructor/dedup/retriever 的
        # experience 具名实例），整体替换会把经验算子顶掉、令 evolver.experience
        # 的具名引用在装配期解析失败。
        cfg["llm"]["default"] = "echo"
        cfg["extractor"]["default"] = "keyword"
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


class TestSdkMode:
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

                result = json.loads(
                    await provider.handle_tool_call("agent_memory_search", {"query": "深色主题"})
                )
                assert any("深色主题" in item["memory"] for item in result["results"]), result
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


# --------------------------------------------------------------------------- #
# _merge_config_over_default：上层 config_dict 深合并到内置 agent 默认装配
# --------------------------------------------------------------------------- #


class TestMergeConfigOverDefault:
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

    def test_each_call_returns_fresh_dict(self, monkeypatch):
        import agent_memory_provider as _amp

        monkeypatch.setattr(_amp, "_agent_default_config", lambda *a, **k: {"globals": {"a": 1}})
        first = _amp._merge_config_over_default({"globals": {"b": 2}})
        second = _amp._merge_config_over_default({"globals": {"c": 3}})
        assert first is not second, "每次返回全新字面量"
        assert first["globals"] == {"a": 1, "b": 2}, "首次合并结果"
        assert second["globals"] == {"a": 1, "c": 3}, "无跨调用可变状态污染"

    def test_experience_segments_follow_experience_space(self):
        """经验段随 experience_space 条件进合并基准（§2.3 开关，2026-10-27）。

        空串 → 基准无经验段（上层显式配 globals.experience 仍尊重）；非空 →
        基准带经验段且落点对齐——检索落点、周期整理与内核写入落点同空间。
        """
        import agent_memory_provider as _amp

        merged = _amp._merge_config_over_default(
            {"globals": {"llm_api_key": "sk-cred"}}, experience_space=""
        )
        assert "experience" not in merged["globals"], "空串：上层未配则无经验段"
        assert "pipeline" not in merged, "空串：无 pipeline 段"

        merged = _amp._merge_config_over_default(
            {"globals": {"experience": {"default_space": "custom"}}}, experience_space=""
        )
        assert merged["globals"]["experience"]["default_space"] == "custom", (
            "空串但上层显式配了 globals.experience：尊重上层（部署配置为准）"
        )

        merged = _amp._merge_config_over_default({}, experience_space="p_agent-memory")
        assert (
            merged["globals"]["experience"]["default_space"] == "p_agent-memory"
        ), "非空：基准带经验段且落点对齐"
        assert "experience" in merged["evolver"], "非空：经验算子组进合并基准"


# --------------------------------------------------------------------------- #
# mem2.0：经验记忆四项接入改动（查经验工具 / add_trajectory / 技能同步 / 周期任务）
# --------------------------------------------------------------------------- #


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
        assert exp["curated_spaces"] == [{"space": "p_agent-memory", "skill": True}], (
            "周期整理空间与技能沉淀空间同步渲染"
        )

    def test_experience_filters_shape(self):
        import agent_memory_provider as _amp

        filters = _amp._experience_filters("software-engineering-devops", ["bash"])
        assert filters == {
            "AND": [
                {"system_metadata.memory_type": "experience"},
                {"system_metadata.category": "software-engineering-devops"},
                {
                    "OR": [
                        {"tier": {"ne": "procedural"}},
                        {"system_metadata.capability": {"in": ["bash"]}},
                    ]
                },
            ]
        }, "三段过滤形态（类型/类别/能力，mem2.0 §2.4）"

    def test_experience_filters_empty_capabilities_only_facts(self):
        import agent_memory_provider as _amp

        filters = _amp._experience_filters("other", [])
        assert filters["AND"][2] == {"tier": {"ne": "procedural"}}, (
            "空能力清单：右支恒假退化为单条件，只返回环境事实（保守口径）；"
            "不能用 in 空列表表达——内核 filter 校验拒绝空列表"
        )

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
        assert call["capabilities"] == [], "未声明能力清单的保守口径"

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
        mem2 = tmp_path / "_mem2"
        (mem2 / "removed-earlier").mkdir(parents=True)
        (mem2 / "removed-earlier" / "SKILL.md").write_text("stale", encoding="utf-8")
        asyncio.run(provider.sync_skills())
        written = (mem2 / "start-service" / "SKILL.md").read_text(encoding="utf-8")
        assert written == "# start-service v2\nnew", "同名取版本号最大的一版"
        assert not (mem2 / "removed-earlier").exists(), "清理不在结果里的目录"
        assert (mem2 / "stale-skill" / "SKILL.md").exists(), "结果内的合规技能保留"

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
        mem2 = tmp_path / "_mem2"
        assert (mem2 / "ok-skill" / "SKILL.md").exists(), "合规技能写入"
        assert not (mem2 / "Bad_Name!").exists(), "不合规名跳过"
        assert not (tmp_path / "local-skill" / "SKILL.md").exists(), (
            "本地已有同名技能跳过（本地优先）"
        )
        assert not (mem2 / "needs-docker").exists(), "清单外工具的技能跳过"
        written_names = [p.name for p in mem2.iterdir() if p.is_dir()]
        assert "escape" not in written_names, "路径逃逸形态不落盘"

    def test_no_rewrite_when_content_unchanged(self, tmp_path):
        provider = _experience_provider(skills_dir=str(tmp_path))
        fake: _FakeExperienceClient = provider._client
        fake._skills = [self._skill("stable-skill", 3, content="# stable\nfixed")]
        target = tmp_path / "_mem2" / "stable-skill" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("# stable\nfixed", encoding="utf-8")
        before = target.stat().st_mtime_ns
        asyncio.run(provider.sync_skills())
        assert target.stat().st_mtime_ns == before, "内容无变化不重写（保留 mtime）"

    def test_takes_no_action_on_fetch_error(self, tmp_path):
        provider = _experience_provider(skills_dir=str(tmp_path))
        provider._client = _FakeExperienceClient(error=RuntimeError("list failed"))
        target = tmp_path / "_mem2" / "keep-me" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("existing", encoding="utf-8")
        with pytest.raises(RuntimeError, match="list failed"):
            asyncio.run(provider.sync_skills())
        assert target.read_text(encoding="utf-8") == "existing", (
            "取不到时不动现有文件，下次再试（§2.5.6）"
        )


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
        assert interval == 604800, "一周一次"

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
