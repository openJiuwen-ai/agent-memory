# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Team documents share ordinary facts and isolate confirmed team conclusions."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.watchdog_impl.local_watchdog import LocalWatchdog
from jiuwen_memory_adapter.jiuwenswarm import base_document_scene
from jiuwen_memory_adapter.jiuwenswarm.memory_scene import MemorySceneContext
from jiuwen_memory_adapter.jiuwenswarm.team_memory import TeamMemoryScene
from tests.unit.jiuwen_memory_adapter.test_agent_memory_provider_unit import (
    AgentMemoryMemoryProvider,
    ApiScope,
    _security_provider,
)
from tests.unit.jiuwen_memory_adapter.test_agent_memory_provider_unit import (
    _offline_in_process_config as _offline_in_process_config,
)

pytestmark = pytest.mark.unit


def test_sdk_team_sharing_isolation_edit_and_watchdog(tmp_path):
    async def run():
        team_key = "repo--" + "a" * 24
        security = _security_provider(ApiScope(org="org", user="user"))
        providers = {
            "leader": AgentMemoryMemoryProvider(
                scene="team",
                project="repo",
                team=team_key,
                sidebar="work",
                member="leader",
                security_provider=security,
                portal={"enabled": False},
            ),
            "member": AgentMemoryMemoryProvider(
                scene="team",
                project="repo",
                team=team_key,
                sidebar="code",
                member="coder",
                security_provider=security,
                portal={"enabled": False},
            ),
            "other": AgentMemoryMemoryProvider(
                scene="team",
                project="repo",
                team="repo--" + "b" * 24,
                security_provider=security,
                portal={"enabled": False},
            ),
            "code": AgentMemoryMemoryProvider(
                scene="code", project="repo", security_provider=security, portal={"enabled": False}
            ),
        }
        try:
            for provider in providers.values():
                await provider.initialize(
                    scope_id="org", user_id="user", agent_id="main", session_id=""
                )
            leader, member = providers["leader"], providers["member"]
            assert (
                await leader.add_team_memories(
                    [
                        {"label": "decision", "content": "confirmed ocean architecture"},
                    ]
                )
                == 1
            )
            path = tmp_path / "memory/teams" / team_key / "MEMORY.md"
            assert "[decision] confirmed ocean architecture" in path.read_text()
            result = json.loads(
                await member.handle_tool_call("team_memory_search", {"query": "ocean"})
            )
            assert result["team"] and not result["project"]
            assert "[decision] confirmed ocean architecture" in result["team"][0]["content"]
            assert "[decision] confirmed ocean architecture" in await member.prefetch("ocean")
            assert "confirmed ocean architecture" not in await providers["other"].prefetch("ocean")
            assert "confirmed ocean architecture" not in await providers["code"].prefetch("ocean")
            edited = json.loads(
                await member.handle_tool_call(
                    "team_memory_edit",
                    {
                        "target": "team",
                        "name": "MEMORY.md",
                        "old_text": "ocean",
                        "new_text": "river",
                    },
                )
            )
            assert edited["count"] == 1
            read = json.loads(
                await leader.handle_tool_call(
                    "team_memory_read",
                    {
                        "target": "team",
                        "name": "MEMORY.md",
                    },
                )
            )
            assert "[decision] confirmed river architecture" in read["units"][0]["content"]
            # Same content elsewhere must not broaden a document read by its content hash.
            await providers["other"].add_team_memories(
                [
                    {"label": "decision", "content": "confirmed river architecture"},
                ]
            )
            read = json.loads(
                await leader.handle_tool_call(
                    "team_memory_read",
                    {
                        "target": "team",
                        "name": "MEMORY.md",
                    },
                )
            )
            assert read["count"] == 1
            for args in [
                {"target": "team", "name": "USER.md"},
                {"target": "project", "name": "../teams/other/MEMORY.md"},
            ]:
                assert "error" in json.loads(
                    await leader.handle_tool_call("team_memory_read", args)
                )
            # 白盒核对看门狗从规范路径恢复归属，不为测试扩充存储公开接口。
            unit = LocalWatchdog._build_unit(  # pylint: disable=protected-access
                None, f"memory/teams/{team_key}/MEMORY.md", "manual", Scope()
            )
            assert unit.system_metadata["project"] == "repo"
            assert unit.system_metadata["team"] == team_key
            assert unit.system_metadata["memory_class"] == "team_memory"
        finally:
            await asyncio.gather(*(p.shutdown() for p in providers.values()))

    asyncio.run(run())


@pytest.mark.parametrize("scene", ["agent", "code", "team"])
def test_team_schemas_are_fresh_and_have_no_identity_arguments(scene):
    provider = AgentMemoryMemoryProvider(scene=scene, project="repo", team="repo--" + "a" * 24)
    schemas = provider.get_tool_schemas()
    if scene == "team":
        assert {tool["name"] for tool in schemas} == {
            "team_memory_" + action for action in ("read", "write", "edit", "search")
        }
        for tool in schemas:
            assert (
                not {"project", "team", "member", "agent_id"}
                & tool["parameters"]["properties"].keys()
            )
    original = schemas[0]["name"]
    schemas[0]["name"] = "mutated"
    assert provider.get_tool_schemas()[0]["name"] == original


@pytest.mark.parametrize("writer_scene", ["agent", "team", "code"])
@pytest.mark.parametrize("explicit", [False, True])
def test_profile_and_project_facts_shared_across_modes(
    tmp_path, monkeypatch, writer_scene, explicit,
):
    import re

    from jiuwen_memory.construction.extractor_impl.agent_llm_extractor import AgentLlmExtractor

    def extract_response(_extractor, messages):
        source = re.search(r"\[ID: ([^\]]+)\]", messages[-1].content).group(1)
        return json.dumps(
            [
                {
                    "source_id": source,
                    "target": "fact",
                    "confidence": 1.0,
                    "tier": "semantic",
                    "content": "user prefers violet",
                    "memory_class": "user_profile",
                    "project_fact": False,
                    "team_internal": False,
                },
                {
                    "source_id": source,
                    "target": "fact",
                    "confidence": 1.0,
                    "tier": "semantic",
                    "content": "ocean project uses sqlite",
                    "memory_class": "long_term_memory",
                    "project_fact": True,
                    "team_internal": False,
                },
            ]
        )

    monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", extract_response)

    async def run():
        security = _security_provider(ApiScope(org="org", user="user"))
        scenes = [
            (writer_scene, "repo"),
            ("agent", "repo"),
            ("code", "repo"),
            ("team", "repo"),
            ("team", "elsewhere"),
        ]
        providers = []
        for scene, project in scenes:
            provider = AgentMemoryMemoryProvider(
                scene=scene,
                project=project,
                team=project + "--" + "a" * 24,
                config_dict={"extractor": {
                    "document_fact": "document_fact", "team_turn": "team_llm",
                }},
                security_provider=security,
                portal={"enabled": False},
            )
            providers.append(provider)
        try:
            for provider in providers:
                await provider.initialize(
                    scope_id="org", user_id="user", agent_id="main", session_id=""
                )
            content = "confirmed preferences and architecture"
            if explicit:
                tool, args = {
                    "agent": ("agent_memory_conclude", {"conclusion": content, "project": "repo"}),
                    "code": ("agent_memory_coding_memory_write", {"content": content}),
                    "team": ("team_memory_write", {"content": content}),
                }[writer_scene]
                result = json.loads(await providers[0].handle_tool_call(tool, args))
                assert "error" not in result, result
            else:
                await providers[0].sync_turn(content, "acknowledged", project="repo")
            assert "user prefers violet" in (tmp_path / "USER.md").read_text()
            assert "ocean project uses sqlite" in (tmp_path / "memory/repo/MEMORY.md").read_text()
            for provider, (_, project) in zip(providers[1:], scenes[1:], strict=True):
                recalled = await provider.prefetch("ocean", project=project)
                assert "user prefers violet" in recalled
                if provider is providers[-1]:
                    assert "ocean project uses sqlite" not in recalled
                else:
                    assert "ocean project uses sqlite" in recalled
        finally:
            await asyncio.gather(*(p.shutdown() for p in providers))

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["hit", "empty", "timeout", "error", "background"])
def test_prefetch_keeps_independent_documents_within_search_budget(monkeypatch, outcome):

    monkeypatch.setattr(base_document_scene, "RECALL_TIMEOUT", 0.5)

    async def run():
        search_finished = asyncio.Event()

        async def search(*args, **kwargs):
            if outcome == "timeout":
                # The documents must start while this search is still pending.
                try:
                    await asyncio.Event().wait()
                finally:
                    search_finished.set()
            if outcome == "error":
                raise RuntimeError("search unavailable")
            return [{"content": "matched fact"}] if outcome == "hit" else []

        async def read(name, scope, **coordinates):
            if outcome == "timeout":
                assert not search_finished.is_set(), "fallback reads started after search timeout"
            target = "profile" if name == "USER.md" else (
                "team" if coordinates.get("team") else "project"
            )
            return [{"content": target + " document"}]

        client = SimpleNamespace(search=AsyncMock(side_effect=search), read_md_file=read)
        context = MemorySceneContext(
            client=client, scope=None, prepare=lambda text: text,
            video_enabled=lambda: False, continuity_enabled=False, continuity_mode="",
            team="repo--" + "a" * 24,
        )
        result = await asyncio.wait_for(
            TeamMemoryScene("repo").prefetch(
                context, "question", background=outcome == "background"
            ),
            timeout=1.0,
        )
        assert "profile document" in result
        if outcome == "hit":
            assert result.count("matched fact") == 2
            assert "project document" not in result and "team document" not in result
        else:
            assert "project document" in result and "team document" in result
        if outcome == "background":
            client.search.assert_not_awaited()
        else:
            assert client.search.await_count == 2

    asyncio.run(run())


async def _initialized_team_provider(team: str = "repo--" + "a" * 24):
    """通过实际初始化入口注入客户端替身，不修改 Provider 的运行状态。"""
    provider = AgentMemoryMemoryProvider(
        scene="team", project="repo", team=team, portal={"enabled": False},
    )
    client = SimpleNamespace(
        initialize=AsyncMock(), add=AsyncMock(return_value=SimpleNamespace(item_id="saved")),
    )
    with patch("agent_memory_provider._build_client", return_value=client):
        await provider.initialize()
    return provider, client


def test_conclusion_policy_rejects_mismatched_host_and_skips_invalid_items():
    async def run():
        provider, client = await _initialized_team_provider("other--" + "a" * 24)
        items = [{"label": "decision", "content": "  confirmed fact  "}]
        with pytest.raises(ValueError, match="bound project"):
            await provider.add_team_memories(items)
        client.add.assert_not_awaited()
        provider, client = await _initialized_team_provider()
        items.extend([{"label": "draft", "content": "unconfirmed"},
                      {"label": "lesson", "content": " "}, None])
        with patch("agent_memory_provider.logger.warning") as warning:
            assert await provider.add_team_memories(items) == 1
        warning.assert_called_once()
        log = warning.call_args.args[0] % warning.call_args.args[1:]
        assert "skipped=3 total=4" in log
        assert all(reason in log for reason in ("invalid_item", "invalid_label", "invalid_content"))
        assert "unconfirmed" not in log and "confirmed fact" not in log
        assert client.add.await_count == 1
        assert client.add.call_args.args[0] == "[decision] confirmed fact"
        metadata = client.add.call_args.kwargs["system_metadata"]
        assert metadata["pipeline"] == "team_conclusion" and metadata["infer"] == "false"
        assert "memory_class" not in metadata

    asyncio.run(run())


@pytest.mark.parametrize("label", ["decision", "lesson", "member", "context"])
def test_team_conclusion_labels_are_visible_without_duplicate_prefixes(label):
    async def run():
        provider, client = await _initialized_team_provider()
        content = f"[{label}] confirmed fact"
        assert await provider.add_team_memories([
            {"label": label, "content": "  confirmed fact  "},
            {"label": label, "content": content},
        ]) == 2
        for call in client.add.call_args_list:
            assert call.args[0] == content
            assert call.kwargs["system_metadata"]["team_label"] == label

    asyncio.run(run())


@pytest.mark.parametrize("items", [[], [{"label": "draft", "content": "private draft"}]])
def test_team_conclusion_empty_or_invalid_batch_does_not_write(items):
    async def run():
        provider, client = await _initialized_team_provider()
        with patch("agent_memory_provider.logger.warning") as warning:
            if items:
                with pytest.raises(ValueError, match="No valid Team conclusions"):
                    await provider.add_team_memories(items)
                warning.assert_called_once()
                assert "private draft" not in str(warning.call_args)
            else:
                assert await provider.add_team_memories(items) == 0
                warning.assert_not_called()
        client.add.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("target,name,team,error", [
    ("unknown", "MEMORY.md", "bound", "target must be"),
    ("profile", "MEMORY.md", "bound", "Profile memory only supports USER.md"),
    ("team", "USER.md", "bound", "Team memory only supports MEMORY.md"),
    ("team", "MEMORY.md", "", "host has not bound a team"),
    ("project", "../MEMORY.md", "bound", "valid date basename"),
    ("project", "2026-02-30.md", "bound", "valid date basename"),
    ("project", "20260101.md", "bound", "valid date basename"),
])
def test_team_document_errors_identify_invalid_argument(target, name, team, error):

    async def run():
        client = SimpleNamespace(read_md_file=AsyncMock())
        context = SimpleNamespace(client=client, team=team)
        with pytest.raises(ValueError, match=error):
            await TeamMemoryScene("repo").handle_tool_call(
                context, "team_memory_read", {"target": target, "name": name},
            )
        client.read_md_file.assert_not_awaited()

    asyncio.run(run())
