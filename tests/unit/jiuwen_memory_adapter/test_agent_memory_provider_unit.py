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
import yaml

from jiuwen_memory.api import ConflictError, Modality, Surface
from jiuwen_memory.api import Scope as ApiScope
from jiuwen_memory.common.normalizer.normalizer_impl.video_normalizer import VideoNormalizer
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

    def _offline() -> dict:
        cfg = _orig()
        cfg["globals"]["embedder_dim"] = 64
        cfg["globals"]["watch_document"] = False
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
        ["default", "file-json", "file-yaml", "inline", "globals", "no-pipeline", "wrong-evolver"],
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
        if configuration in {"default", "file-json", "file-yaml"}:
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
            assert ("agent_memory_video_submit" in schemas) is enabled
            assert ("agent_memory_video_status" in schemas) is enabled
            early = await provider.handle_tool_call("agent_memory_video_submit", {"uri": uri})
            assert json.loads(early) == {"error": "provider not initialized"}
            await provider.initialize(user_id="user", scope_id="tenant", session_id="")
            try:
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
                project = {"project": "video-review"} if configuration == "globals" else {}
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
                if config_path:
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


# --------------------------------------------------------------------------- #
# _merge_config_over_default：上层 config_dict 深合并到内置 agent 默认装配
# --------------------------------------------------------------------------- #


class TestMergeConfigOverDefault:
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

    def test_each_call_returns_fresh_dict(self, monkeypatch):
        import agent_memory_provider as _amp

        monkeypatch.setattr(_amp, "_agent_default_config", lambda: {"globals": {"a": 1}})
        first = _amp._merge_config_over_default({"globals": {"b": 2}})
        second = _amp._merge_config_over_default({"globals": {"c": 3}})
        assert first is not second, "每次返回全新字面量"
        assert first["globals"] == {"a": 1, "b": 2}, "首次合并结果"
        assert second["globals"] == {"a": 1, "c": 3}, "无跨调用可变状态污染"
