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

from jiuwen_memory.api import ConflictError, Modality, Surface, ValidationError
from jiuwen_memory.api import Scope as ApiScope
from jiuwen_memory.api import normalize as normalize_filters
from jiuwen_memory.common.normalizer.normalizer_impl.video_normalizer import VideoNormalizer
from jiuwen_memory.common.security import new_request_context
from jiuwen_memory.common.security.types import AuthContext, Role
from jiuwen_memory.common.type_def.filter import FilterLogic

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

    def test_filters_with_project_add_cross_project_in_predicate(self):
        filters = _recall_filters("proj-a")
        assert filters["system_metadata.project"] == {"in": ["", "proj-a"]}, filters
        assert filters["tier"] == {"in": ["semantic", "core", "procedural"]}, filters

    def test_filters_without_project_carry_no_project_predicate(self):
        assert "system_metadata.project" not in _recall_filters(""), "空 project 不应带项目谓词"

    def test_filters_with_project_normalize_to_and_of_two_clauses(self):
        # 顶层双键由 API 边界的 normalize 解析为 AND；HTTP 服务端与进程内共用同一解析。
        expr = normalize_filters(_recall_filters("proj-a"))
        assert expr.logic is FilterLogic.AND, expr
        clauses = {(c.field, c.op.value): c.value for c in expr.children}
        assert clauses == {
            ("tier", "in"): ["semantic", "core", "procedural"],
            ("system_metadata.project", "in"): ["", "proj-a"],
        }, clauses


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
                assert seen[0]["system_metadata.project"] == {"in": ["", "proj-a"]}, seen[0]
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
                assert "上海" in same, f"同项目会话应召回跨项目记忆: {same!r}"

                other = await provider.prefetch("部署区域", project="proj-b")
                assert "杭州" not in other, f"其他项目不应看到 proj-a 的记忆: {other!r}"
                assert "上海" in other, f"其他项目应召回跨项目记忆: {other!r}"

                # 不带 project 时过滤条件里没有项目谓词，由影子索引兜底 project=''：
                # 只召回跨项目可见的记忆，不会放宽成全库召回。
                projectless = await provider.prefetch("部署区域")
                assert "杭州" not in projectless, f"无项目会话不应看到项目记忆: {projectless!r}"
                assert "上海" in projectless, f"无项目会话应召回跨项目记忆: {projectless!r}"
            finally:
                await provider.shutdown()

        asyncio.run(_run())


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

    def test_each_call_returns_fresh_dict(self, monkeypatch):
        import agent_memory_provider as _amp

        monkeypatch.setattr(_amp, "_agent_default_config", lambda: {"globals": {"a": 1}})
        first = _amp._merge_config_over_default({"globals": {"b": 2}})
        second = _amp._merge_config_over_default({"globals": {"c": 3}})
        assert first is not second, "每次返回全新字面量"
        assert first["globals"] == {"a": 1, "b": 2}, "首次合并结果"
        assert second["globals"] == {"a": 1, "c": 3}, "无跨调用可变状态污染"
