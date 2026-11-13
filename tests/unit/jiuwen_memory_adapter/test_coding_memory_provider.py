# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Code tools bind projects; document and shadow persistence survive provider restart."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwen_memory_adapter.jiuwenswarm.coding_memory import TOOL_PREFIX

# Re-export the autouse fixture so pytest installs the offline SDK configuration.
from tests.unit.jiuwen_memory_adapter.test_agent_memory_provider_unit import (
    AgentMemoryMemoryProvider,
    ApiScope,
    _security_provider,
)
from tests.unit.jiuwen_memory_adapter.test_agent_memory_provider_unit import (
    _offline_in_process_config as _offline_in_process_config,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def code_provider(monkeypatch):
    provider = AgentMemoryMemoryProvider(scene="code", project="repo-a")
    client = SimpleNamespace(
        initialize=AsyncMock(),
        is_unavailable=lambda: False,
        search=AsyncMock(return_value=[{"content": "match", "score": 1}]),
        add=AsyncMock(return_value=SimpleNamespace(item_id="new")),
        read_md_file=AsyncMock(return_value=[]), edit_md_file=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        f"{AgentMemoryMemoryProvider.__module__}._build_client", lambda *args, **kwargs: client,
    )
    asyncio.run(provider.initialize())
    return provider, client


def test_code_tools_bind_project_and_report_actual_write_result(code_provider, monkeypatch):
    provider, client = code_provider
    video_enabled = Mock(return_value=True)
    monkeypatch.setattr(provider, "_video_tools_enabled", video_enabled)

    async def run():
        schemas = {tool["name"]: tool for tool in provider.get_tool_schemas()}
        assert set(schemas) == {
            TOOL_PREFIX + action for action in ("read", "write", "edit", "search")}
        assert all("project" not in tool["parameters"]["properties"] for tool in schemas.values())
        await provider.handle_tool_call(
            TOOL_PREFIX + "search", {"query": "fact", "project": "other"},
        )
        assert client.search.call_args.kwargs["filters"] == {
            "system_metadata.project": {"eq": "repo-a"}}
        for outcome, expected in [(None, "error"), (SimpleNamespace(item_id=None), "stored"),
                                  (SimpleNamespace(item_id="new"), "stored")]:
            client.add.return_value = outcome
            result = json.loads(await provider.handle_tool_call(
                TOOL_PREFIX + "write", {"content": "fact", "project": "other"}))
            assert expected in result
            if outcome is not None:
                assert result["stored"] is bool(outcome.item_id)
            assert client.add.call_args.kwargs["system_metadata"] == {
                "infer": "true", "project": "repo-a"}
        for name in (
            "agent_memory_conclude", "agent_memory_video_submit", "agent_memory_video_status",
        ):
            result = json.loads(await provider.handle_tool_call(name, {}))
            assert "error" in result
        video_enabled.assert_not_called()

    asyncio.run(run())


def test_code_read_edit_allow_only_bound_document_basenames(code_provider):
    provider, client = code_provider

    async def run():
        await provider.handle_tool_call(TOOL_PREFIX + "read", {
            "md_filename": "USER.md", "project": "other", "offset": 2, "limit": 3})
        assert client.read_md_file.call_args.kwargs == {
            "project": "", "line_range": (2, 4),
        }
        await provider.handle_tool_call(TOOL_PREFIX + "edit", {
            "md_filename": "MEMORY.md", "old_text": "before", "new_text": "after"})
        assert client.edit_md_file.call_args.kwargs["project"] == "repo-a"
        for name in ("../USER.md", "other/MEMORY.md", "topic.md"):
            response = await provider.handle_tool_call(TOOL_PREFIX + "read", {"md_filename": name})
            assert "error" in json.loads(response)

    asyncio.run(run())


def test_prefetch_profile_search_fallback_and_background(code_provider):
    provider, client = code_provider

    async def run():
        async def read(filename, scope, **kw):
            return [{"content": "profile" if filename == "USER.md" else "fallback"}]
        client.read_md_file.side_effect = read
        assert "match" in await provider.prefetch("question")
        client.search.side_effect = TimeoutError()
        content = await provider.prefetch("question")
        assert "profile" in content and "fallback" in content
        client.search.reset_mock()
        for query, background in [("", False), ("heartbeat", True)]:
            assert "fallback" in await provider.prefetch(query, background=background)
            client.search.assert_not_called()
        await provider.sync_turn("user", "assistant", project="other")
        assert client.add.call_args.kwargs["system_metadata"]["project"] == "repo-a"

    asyncio.run(run())


def test_sdk_project_isolation_profile_sharing_edit_and_restart(tmp_path, monkeypatch):
    async def run():
        from jiuwen_memory.construction.extractor_impl.keyword_extractor import KeywordExtractor

        original_extract = KeywordExtractor.extract

        def classify(self, units, **kwargs):
            derived = original_extract(self, units, **kwargs)
            for unit in derived:
                if unit.content.startswith("User prefers"):
                    unit.system_metadata["memory_class"] = "user_profile"
            return derived

        monkeypatch.setattr(KeywordExtractor, "extract", classify)

        async def open_provider(scene="code", project="repo-a", user="user", agent="main_agent"):
            provider = AgentMemoryMemoryProvider(
                scene=scene, project=project,
                security_provider=_security_provider(ApiScope(org="org", user=user)))
            await provider.initialize(user_id=user, scope_id="org", agent_id=agent, session_id="")
            return provider

        provider = await open_provider()
        try:
            result = json.loads(await provider.handle_tool_call(
                TOOL_PREFIX + "write", {"content": "repository uses Python"},
            ))
            assert result["stored"] is True
            assert (tmp_path / "memory/repo-a/MEMORY.md").exists()
            await provider.handle_tool_call(TOOL_PREFIX + "write",
                                            {"content": "User prefers concise answers"})
            assert (tmp_path / "USER.md").exists()
            await provider.handle_tool_call(TOOL_PREFIX + "edit", {
                "md_filename": "MEMORY.md", "old_text": "Python", "new_text": "Rust"})
            assert "Rust" in (tmp_path / "memory/repo-a/MEMORY.md").read_text()
        finally:
            await provider.shutdown()
        for scene, project, expected in [
            ("agent", "repo-a", False), ("code", "repo-b", False), ("code", "repo-a", True),
        ]:
            provider = await open_provider(scene, project)
            try:
                text = await provider.prefetch("User prefers concise answers repository Rust")
                assert "concise" in text
                assert ("Rust" in text) == expected
            finally:
                await provider.shutdown()

        provider = await open_provider("agent")
        try:
            await provider.handle_tool_call(
                "agent_memory_conclude", {"conclusion": "User prefers dark theme"},
            )
        finally:
            await provider.shutdown()
        for user, agent, shared in [
            ("user", "main_agent", True),
            ("user", "review_agent", False),
            ("another-user", "main_agent", False),
        ]:
            for scene in ("agent", "code"):
                provider = await open_provider(scene, user=user, agent=agent)
                try:
                    recalled = await provider.prefetch("User prefers dark theme repository Rust")
                    assert ("dark theme" in recalled) == shared
                    assert ("Rust" in recalled) == (shared and scene == "code")
                    if scene == "code":
                        profile = await provider.handle_tool_call(
                            TOOL_PREFIX + "read", {"md_filename": "USER.md"})
                        assert ("dark theme" in profile) == shared
                finally:
                    await provider.shutdown()

    asyncio.run(run())


def test_sdk_watchdog_tracks_manual_project_document_edit(tmp_path):
    async def run():
        provider = AgentMemoryMemoryProvider(
            scene="code", project="watched", config_dict={"globals": {"watch_document": True}},
            security_provider=_security_provider(ApiScope(org="org", user="user")))
        await provider.initialize(user_id="user", scope_id="org", agent_id="shared", session_id="")
        try:
            await provider.handle_tool_call(
                TOOL_PREFIX + "write", {"content": "manual edit before"})
            path = tmp_path / "memory/watched/MEMORY.md"
            # The existing watcher deliberately ignores its first second of events.
            await asyncio.sleep(1.1)
            path.write_text(path.read_text().replace("manual edit before", "manual edit after"))
            for _ in range(60):
                result = await provider.handle_tool_call(
                    TOOL_PREFIX + "read", {"md_filename": "MEMORY.md"},
                )
                if "manual edit after" in result:
                    break
                await asyncio.sleep(0.1)
            assert "manual edit after" in result
        finally:
            await provider.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("source", ["json", "yaml", "inline", "default"])
def test_code_document_override_preserves_configuration_baseline(tmp_path, monkeypatch, source):
    config = {"globals": {"write_document": False, "custom_option": "kept"}}
    path = tmp_path / f"config.{source}"
    path.write_text(json.dumps(config) if source != "yaml" else
                    "globals:\n  write_document: false\n  custom_option: kept\n")
    options = {"config_path": str(path)} if source != "default" else {}
    if source == "inline":
        options["config_dict"] = config
    expected = {"builtin": True} if source in {"inline", "default"} else {}
    if source != "default":
        expected["custom_option"] = "kept"
    module = AgentMemoryMemoryProvider.__module__
    monkeypatch.setattr(f"{module}._agent_default_config",
                        lambda: {"globals": {"builtin": True, "write_document": False}})
    assemble = Mock(return_value=SimpleNamespace(api=object(), start_background=AsyncMock()))
    monkeypatch.setattr(f"{module}.assemble_runtime", assemble)
    for scene in ("agent", "code"):
        # This unit test isolates configuration assembly from the optional Portal.
        provider = AgentMemoryMemoryProvider(
            scene=scene, security_provider=lambda: None, portal={"enabled": False}, **options,
        )
        asyncio.run(provider.initialize())
        assert assemble.call_args.kwargs["config"] == {
            "globals": {**expected, "write_document": scene == "code"}}
    assert config["globals"]["write_document"] is False
