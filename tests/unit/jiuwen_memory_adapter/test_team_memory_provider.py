# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Team documents share ordinary facts and isolate confirmed team conclusions."""

import asyncio
import json
from contextlib import closing
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwen_memory.common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Scope, Segment
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import SqliteDocumentShadowIndex
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


def test_sdk_team_sharing_isolation_edit_and_watchdog(tmp_path, monkeypatch):
    from jiuwen_memory.api import Context, PermissionDeniedError
    from jiuwen_memory.api.collective import Dimension, Member, bind_collective_context
    from jiuwen_memory.common.llm.llm_impl.echo_llm import EchoLLM
    from jiuwen_memory.common.security.types import Role

    def chat(_self, messages, **_kwargs):
        if len(messages) != 2 or "source_segments" not in messages[1].content:
            return messages[-1].content
        segment = json.loads(messages[1].content)["source_segments"][-1]
        is_daily = "milestone" in segment["text"]
        destination = "project" if is_daily else "team"
        if segment["id"] == "agent-lesson":
            destination = "agent"
        return json.dumps([{
            "content": segment["text"], "kind": "daily" if is_daily else "experience",
            "source_id": segment["id"], "evidence": segment["text"],
            "destination": destination,
        }])

    monkeypatch.setattr(EchoLLM, "chat", chat)

    async def run():
        caller = Scope(org="org", user="user")
        admin = _security_provider(Scope(org="org", user="admin"), role=Role.ADMIN)()
        coordinates = {"project": "repo", "team": "reviewers"}
        leader, member = [
            AgentMemoryMemoryProvider(
                scene="team", project="repo", collective_coordinates=coordinates,
                user_id="user", agent_id=agent,
                security_provider=_security_provider(caller), portal={"enabled": False},
            )
            for agent in ("leader", "coder")
        ]
        try:
            for provider, agent in ((leader, "leader"), (member, "coder")):
                await provider.initialize(
                    scope_id="org", user_id="user", agent_id=agent, session_id="",
                )
            authors = [Scope(org="org", user="user", agent=agent) for agent in ("leader", "coder")]
            dimension = Dimension("org", "", "team", "reviewers")
            api = leader.runtime.api
            security = bind_collective_context(_security_provider(caller)(), coordinates)
            roster = [Member(authors[0], "manager"), Member(authors[1])]
            await asyncio.to_thread(api.init_collective_dimension, dimension, roster, security=admin)
            lesson = "Verify rollback snapshots before a release"
            [agent_unit] = await api.add_async("", authors[1], security=security, system_metadata={
                "sources": [{"id": "agent-lesson", "role": "assistant", "text": lesson}],
            })
            assert agent_unit.system_metadata["collective_destination"] == "agent"
            assert agent_unit.system_metadata["memory_class"] == "agent_memory"
            assert agent_unit.system_metadata["shareable"] is True
            for tool in member.get_tool_schemas():
                target_schema = tool["parameters"]["properties"].get("target")
                if target_schema is not None:
                    assert target_schema["enum"] == ["profile", "project", "team", "agent"]
            agent_target = {"target": "agent", "name": "MEMORY.md"}
            read = json.loads(await member.handle_tool_call("team_memory_read", agent_target))
            assert [unit["content"] for unit in read["units"]] == [lesson]
            found = json.loads(await member.handle_tool_call(
                "team_memory_search", {"target": "agent", "query": "rollback snapshots"},
            ))
            assert [hit["content"] for hit in found["hits"]] == [lesson]
            assert lesson in await member.prefetch("rollback snapshots")
            changed = json.loads(await member.handle_tool_call("team_memory_edit", {
                **agent_target, "old_text": "snapshots", "new_text": "checkpoints",
            }))
            assert changed["units"][0]["content"] == lesson.replace("snapshots", "checkpoints")
            rejected = json.loads(await member.handle_tool_call(
                "team_memory_search", {"target": "personal", "query": "rollback checkpoints"},
            ))
            assert rejected["error"] == "Invalid collective memory target"
            await leader.sync_turn("", "", sources=[{
                "id": "accepted", "role": "assistant", "text": "The ocean architecture was approved",
            }])
            await leader.sync_turn("", "", sources=[{
                "id": "duplicate", "role": "assistant", "text": "The ocean architecture was approved",
            }])
            path = tmp_path / "memory/team_memory/teams/reviewers/MEMORY.md"
            assert path.read_text(encoding="utf8").count("The ocean architecture") == 1
            experience = "ocean deployment experience"
            # Seed the other pipeline's result in the shared index before searching.
            tokenizer = WhitespaceTokenizer()
            shadow = SqliteDocumentShadowIndex(
                str(tmp_path / "memory/.shadow/shadow.db"),
                tokenizer=tokenizer, embedder=HashingEmbedder(tokenizer, dim=64),
            )
            with closing(shadow):
                await asyncio.to_thread(shadow.insert_units, authors[1], [MemoryUnit(
                    id="experience", scope=authors[1], tier=MemoryTier.PROCEDURAL, segments=[Segment(experience)],
                    system_metadata={"memory_type": "experience"},
                )])
            raw = await asyncio.to_thread(api.search, "ocean", Context(authors[1]), security=security)
            assert not raw.errors, raw.errors
            assert experience not in {item.content for item in raw.items}
            result = json.loads(
                await member.handle_tool_call("team_memory_search", {"query": "ocean"})
            )
            assert any("ocean architecture" in hit["content"] for hit in result["hits"])
            isolated_scope = replace(member.bound_scope(), session="isolated")
            with patch.object(member, "bound_scope", return_value=isolated_scope):
                denied = json.loads(
                    await member.handle_tool_call("team_memory_search", {"query": "ocean"})
                )
                assert "authenticated subject's scope" in denied["error"]
                assert await member.prefetch("ocean") == ""
            elsewhere = bind_collective_context(
                _security_provider(caller)(), {"project": "elsewhere", "team": "reviewers"},
            )
            across_projects = await asyncio.to_thread(
                api.search, "ocean", Context(authors[1]), security=elsewhere,
            )
            assert any("ocean architecture" in item.content for item in across_projects.items)
            assert not (await asyncio.to_thread(
                api.search, "ocean", Context(authors[1]), security=_security_provider(caller)(),
            )).items

            edited = json.loads(await member.handle_tool_call("team_memory_edit", {
                "target": "team", "name": "MEMORY.md", "old_text": "ocean", "new_text": "river",
            }))
            assert edited["count"] == 1
            unit = await asyncio.to_thread(
                api.get, edited["units"][0]["item_id"], authors[0], security=security,
            )
            assert unit.scope == authors[0]
            assert unit.system_metadata["shareable"] is False
            assert unit.system_metadata["team"] == "reviewers"
            assert unit.system_metadata["project"] == unit.system_metadata["group"] == ""
            assert unit.supersedes
            assert "sources" not in unit.system_metadata and "source_role" not in unit.system_metadata

            await asyncio.to_thread(api.remove_collective_members, dimension, [authors[1]], security=admin)
            await asyncio.to_thread(api.init_collective_dimension, dimension, roster, security=admin)
            with pytest.raises(PermissionDeniedError):
                await asyncio.to_thread(
                    api.read_md_file, Context(authors[1], {"collective_target": "team"}),
                    "MEMORY.md", security=security,
                )
            assert not (await asyncio.to_thread(
                api.search, "river", Context(authors[1]), security=security,
            )).items
            project = Dimension("org", "", "project", "repo")
            await asyncio.to_thread(api.init_collective_dimension, project, roster, security=admin)
            diary_name = f"{date.today().isoformat()}.md"
            diary_path = tmp_path / "memory/repo/daily_memory" / diary_name
            diary_path.parent.mkdir(parents=True, exist_ok=True)
            diary_path.write_text("# Checklist\nrelease on Friday\n\n", encoding="utf8")
            with closing(shadow):
                await asyncio.to_thread(shadow.insert_units, authors[0], [MemoryUnit(
                    id="ordinary-diary", scope=authors[0], segments=[Segment("release on Friday")],
                    system_metadata={
                        "memory_class": "daily_memory", "project": "repo",
                        "md_filename": diary_path.relative_to(tmp_path).as_posix(),
                    },
                )])
            target = {"target": "project", "name": diary_name}
            diary_read = json.loads(await member.handle_tool_call("team_memory_read", target))
            assert diary_read["units"][0]["content"] == "release on Friday"
            diary_edit = json.loads(await member.handle_tool_call(
                "team_memory_edit", {**target, "old_text": "Friday", "new_text": "Monday"},
            ))
            assert diary_edit["units"][0]["content"] == "release on Monday", diary_edit
            assert "release on Monday" in diary_path.read_text(encoding="utf-8")
            await leader.sync_turn("", "", sources=[{
                "id": "milestone", "role": "assistant", "text": "The milestone was delivered on Tuesday",
            }])
            collective_diary = tmp_path / "memory/team_memory/projects/repo/daily_memory" / diary_name
            assert "delivered on Tuesday" in collective_diary.read_text(encoding="utf8")
            read = json.loads(await member.handle_tool_call(
                "team_memory_read", {"target": "project", "name": diary_name},
            ))
            assert {unit["content"] for unit in read["units"]} == {
                "release on Monday", "The milestone was delivered on Tuesday",
            }
            ordinary_diary = await asyncio.to_thread(
                api.read_md_file, Context(authors[0], {"coords": {"project": "repo"}}),
                diary_name, security=_security_provider(caller)(),
            )
            assert [unit.content for unit in ordinary_diary] == ["release on Monday"]
            updated = json.loads(await member.handle_tool_call("team_memory_edit", {
                "target": "project", "name": diary_name, "old_text": "Tuesday", "new_text": "Wednesday",
            }))
            version = await asyncio.to_thread(
                api.get, updated["units"][0]["item_id"], authors[0], security=security,
            )
            assert version.system_metadata["memory_class"] == "daily_memory"
            assert version.system_metadata["collective_destination"] == "project"
            assert version.system_metadata["shareable"] is False
            assert version.supersedes
            [shared] = await api.add_async("", authors[0], security=security, system_metadata={
                "sources": [{"id": "shared", "role": "assistant", "text": "Shared ocean team rule"}],
                "shareable": True,
            })
            assert shared.system_metadata["shareable"] is True
            project_security = bind_collective_context(_security_provider(caller)(), {"project": "repo"})
            explicit = await asyncio.to_thread(api.search, "ocean", Context(authors[0]), security=project_security)
            assert shared.content in {item.content for item in explicit.items}
            work = AgentMemoryMemoryProvider(
                collective_coordinates={"project": "repo"},
                security_provider=_security_provider(caller), portal={"enabled": False},
            )
            try:
                await work.initialize(
                    scope_id="org", user_id="user", agent_id="leader", session_id="",
                )
                assert "Shared ocean team rule" not in await work.prefetch("ocean", project="repo")
            finally:
                await work.shutdown()
            # Verify disk recovery without starting an observer thread in the SDK test.
            inferred = LocalWatchdog._build_unit(  # pylint: disable=protected-access
                None, collective_diary.relative_to(tmp_path).as_posix(), "manual", authors[0],
            )
            assert inferred.system_metadata["shareable"] is False
            assert inferred.system_metadata["memory_class"] == "daily_memory"
            assert inferred.system_metadata["collective_destination"] == "project"
            assert inferred.system_metadata["project"] == "repo"
        finally:
            await asyncio.gather(leader.shutdown(), member.shutdown())

    asyncio.run(run())


@pytest.mark.parametrize("scene", ["agent", "code", "team"])
def test_team_schemas_are_fresh_and_have_no_identity_arguments(scene):
    coordinates = {"project": "repo"}
    if scene == "team":
        coordinates["team"] = "repo--" + "a" * 24
    provider = AgentMemoryMemoryProvider(
        scene=scene, project="repo", collective_coordinates=coordinates,
    )
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

    from jiuwen_memory.api.collective import Dimension, Member
    from jiuwen_memory.common.llm.llm_impl.echo_llm import EchoLLM
    from jiuwen_memory.common.security.types import Role
    from jiuwen_memory.construction.extractor_impl.agent_llm_extractor import AgentLlmExtractor

    def collect_response(_llm, messages, **_kwargs):
        if len(messages) != 2 or "source_segments" not in messages[1].content:
            return messages[-1].content
        payload = json.loads(messages[-1].content)
        source = payload["source_segments"][0]
        rows = [{
            "source_id": source["id"], "kind": "experience", "destination": "project",
            "evidence": source["text"], "content": "ocean project uses sqlite",
        }]
        if source["role"] == "user":
            rows.append({
                "source_id": source["id"], "kind": "user_fact", "destination": "user_profile",
                "evidence": "user prefers violet", "content": "user prefers violet",
            })
        return json.dumps(rows)

    monkeypatch.setattr(EchoLLM, "chat", collect_response)

    def extract_response(_extractor, messages, **_kwargs):
        if "source_segments" in messages[-1].content:
            return collect_response(None, messages)
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
        admin = _security_provider(Scope(org="org", user="admin"), role=Role.ADMIN)()
        scenes = [
            (writer_scene, "repo"),
            ("agent", "repo"),
            ("code", "repo"),
            ("team", "repo"),
            ("team", "elsewhere"),
        ]
        providers = []
        for scene, project in scenes:
            coordinates = {"project": project}
            if scene == "team":
                coordinates["team"] = project + "--" + "a" * 24
            provider = AgentMemoryMemoryProvider(
                scene=scene,
                project=project,
                collective_coordinates=coordinates,
                config_dict={"extractor": {"document_fact": "document_fact"}},
                security_provider=security,
                portal={"enabled": False},
            )
            providers.append(provider)
        try:
            for provider in providers:
                await provider.initialize(
                    scope_id="org", user_id="user", agent_id="main", session_id=""
                )
            contributor = Scope(org="org", user="user", agent="main")
            api = providers[0].runtime.api
            await asyncio.to_thread(
                api.init_collective_dimension, Dimension("org", "", "project", "repo"),
                [Member(contributor, "manager")], security=admin,
            )
            if explicit and writer_scene == "team":
                # An explicit agent-authored conclusion cannot generate a human profile.
                await providers[0].sync_turn("", "", sources=[
                    {"id": "initial", "role": "user", "text": "user prefers violet"},
                ])
            content = "confirmed preferences and architecture"
            if explicit:
                tool, args = {
                    "agent": ("agent_memory_conclude", {"conclusion": content, "project": "repo"}),
                    "code": ("agent_memory_coding_memory_write", {"content": content}),
                    "team": ("team_memory_write", {"content": content}),
                }[writer_scene]
                result = json.loads(await providers[0].handle_tool_call(tool, args))
                assert "error" not in result, result
            elif writer_scene == "team":
                await providers[0].sync_turn("", "", sources=[
                    {"id": "initial", "role": "user", "text": "user prefers violet"},
                    {"id": "result", "role": "assistant", "text": "ocean project uses sqlite"},
                ])
            else:
                await providers[0].sync_turn(content, "acknowledged", project="repo")
            assert "user prefers violet" in (tmp_path / "USER.md").read_text(encoding="utf8")
            project_dir = "memory/team_memory/projects/repo" if writer_scene == "team" else "memory/repo"
            assert "ocean project uses sqlite" in (tmp_path / project_dir / "MEMORY.md").read_text(encoding="utf8")
            for provider, (scene, project) in zip(providers[1:], scenes[1:], strict=True):
                recalled = await provider.prefetch("ocean", project=project)
                if writer_scene == "team" and scene != "team":
                    is_code = provider is providers[2]
                    read_tool = "agent_memory_coding_memory_read" if is_code else "agent_memory_read_md_file"
                    read = await provider.handle_tool_call(read_tool, {"md_filename": "USER.md"})
                    assert "user prefers violet" in read, read
                    search_tool = "agent_memory_coding_memory_search" if is_code else "agent_memory_search"
                    search = await provider.handle_tool_call(search_tool, {"query": "ocean", "project": project})
                    assert "ocean project uses sqlite" in search, search
                    profile = json.loads(read)
                    recalled += "".join(block["memory"] for block in profile["blocks"])
                assert "user prefers violet" in recalled
                if provider is providers[-1]:
                    assert "ocean project uses sqlite" not in recalled
                else:
                    assert "ocean project uses sqlite" in recalled
            if writer_scene == "team":
                work = providers[1]
                if not explicit:
                    for requested, background in (("elsewhere", False), ("repo", True)):
                        recalled = await work.prefetch(
                            "ocean", project=requested, background=background,
                        )
                        assert "ocean project uses sqlite" not in recalled
                    search = await work.handle_tool_call(
                        "agent_memory_search", {"query": "ocean", "project": "elsewhere"},
                    )
                    assert "ocean project uses sqlite" not in search
                read = json.loads(await work.handle_tool_call(
                    "agent_memory_read_md_file", {"md_filename": "USER.md"},
                ))
                old_id = read["blocks"][0]["item_id"]
                edited = json.loads(await work.handle_tool_call("agent_memory_edit_md_file", {
                    "md_filename": "USER.md", "old_text": "violet", "new_text": "indigo",
                }))
                assert edited["units"][0]["memory"] == "user prefers indigo", edited
                new = await asyncio.to_thread(
                    api.get, edited["units"][0]["item_id"], Scope(org="org", user="user"),
                    security=security(),
                )
                assert new.supersedes == old_id and new.scope.agent == ""
        finally:
            await asyncio.gather(*(p.shutdown() for p in providers))

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["hit", "empty", "timeout", "error", "background"])
def test_prefetch_keeps_independent_documents_within_search_budget(monkeypatch, caplog, outcome):

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
            target = coordinates["target"]
            return [{"content": target + " document"}]

        client = SimpleNamespace(search=AsyncMock(side_effect=search), read_md_file=read)
        context = MemorySceneContext(
            client=client, scope=None, prepare=lambda text: text,
            video_enabled=lambda: False, continuity_enabled=False, continuity_mode="",
            team="repo--" + "a" * 24, collective={"project": "repo", "team": "reviewers"},
        )
        result = await asyncio.wait_for(
            TeamMemoryScene("repo").prefetch(
                context, "question", background=outcome == "background"
            ),
            timeout=1.0,
        )
        assert "profile document" in result
        if outcome == "hit":
            assert result.count("matched fact") == 1
            assert "project document" not in result and "team document" not in result
        else:
            assert "matched fact" not in result
        if outcome == "background":
            client.search.assert_not_awaited()
        else:
            assert client.search.await_count == 3
            if outcome != "hit":
                assert "project document" in result and "team document" in result

    asyncio.run(run())
    if outcome == "error":
        assert "search project" in caplog.text
        assert "search team" in caplog.text


async def _initialized_team_provider(team: str = "repo--" + "a" * 24):
    """通过实际初始化入口注入客户端替身，不修改 Provider 的运行状态。"""
    provider = AgentMemoryMemoryProvider(
        scene="team", project="repo", collective_coordinates={"project": "repo", "team": team},
        portal={"enabled": False},
    )
    client = SimpleNamespace(
        initialize=AsyncMock(), add=AsyncMock(return_value=SimpleNamespace(item_id="saved")),
        bind_collective_coordinates=lambda _coordinates: None, is_unavailable=lambda: False,
    )
    with patch("agent_memory_provider._build_client", return_value=client):
        await provider.initialize(session_id="")
    return provider, client


def test_explicit_conclusion_uses_agent_source_and_bound_context():
    async def run():
        provider, client = await _initialized_team_provider()
        response = json.loads(await provider.handle_tool_call("team_memory_write", {
            "content": "  confirmed fact  ",
        }))
        client.add.assert_awaited_once()
        assert response == {"stored": True, "item_id": "saved"}
        assert client.add.call_args.args[0] == ""
        options = client.add.call_args.kwargs
        assert options == {"system_metadata": {
            "infer": True,
            "sources": [{"id": "tool", "role": "assistant", "text": "confirmed fact"}],
        }}

    asyncio.run(run())


@pytest.mark.parametrize("label", ["decision", "lesson", "member", "context"])
def test_explicit_conclusion_submits_text_for_extraction_without_direct_storage(label):
    async def run():
        provider, client = await _initialized_team_provider()
        text = f"[{label}] confirmed fact"
        await provider.handle_tool_call("team_memory_write", {"content": text})
        assert client.add.call_args.args[0] == ""
        assert client.add.call_args.kwargs["system_metadata"]["sources"][0]["text"] == text

    asyncio.run(run())


@pytest.mark.parametrize("content", ["", "  ", None])
def test_empty_explicit_conclusion_does_not_write(content):
    async def run():
        provider, client = await _initialized_team_provider()
        assert "error" in json.loads(await provider.handle_tool_call("team_memory_write", {"content": content}))
        client.add.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("target,name,team,error", [
    ("unknown", "MEMORY.md", "bound", "document target"),
    ("profile", "MEMORY.md", "bound", "Invalid filename"),
    ("team", "USER.md", "bound", "Invalid filename"),
    ("team", "MEMORY.md", "", "unavailable in this context"),
    ("project", "../MEMORY.md", "bound", "Invalid filename"),
    ("project", "2026-02-30.md", "bound", "Invalid filename"),
    ("project", "20260101.md", "bound", "Invalid filename"),
])
def test_team_document_errors_identify_invalid_argument(target, name, team, error):

    async def run():
        client = SimpleNamespace(read_md_file=AsyncMock())
        context = SimpleNamespace(
            client=client, collective={"team": team} if team else {},
        )
        with pytest.raises(ValueError, match=error):
            await TeamMemoryScene("repo").handle_tool_call(
                context, "team_memory_read", {"target": target, "name": name},
            )
        client.read_md_file.assert_not_awaited()

    asyncio.run(run())


def test_team_project_diary_and_document_fallback():
    async def run():
        async def read(filename, _scope, *, target, **kwargs):
            if filename == "USER.md":
                return [{"content": "user profile"}]
            if filename == "2026-11-20.md":
                return [{"content": "project diary"}]
            return [{"content": f"fallback for {target}"}]

        async def search(_query, _scope, *, filters, **_kwargs):
            if filters.get("system_metadata.collective_destination") == {"eq": "team"}:
                return [{"content": "indexed team memory"}]
            return []

        client = SimpleNamespace(
            read_md_file=AsyncMock(side_effect=read), search=AsyncMock(side_effect=search),
        )
        context = SimpleNamespace(
            client=client, collective={"project": "repo", "team": "stable"}, scope=object(),
            prepare=lambda text: text,
        )
        scene = TeamMemoryScene("repo")
        result = json.loads(await scene.handle_tool_call(
            context, "team_memory_read", {"target": "project", "name": "2026-11-20.md"},
        ))
        assert result["units"] == [{"content": "project diary"}]
        prefetched = await scene.prefetch(context, "query")
        assert "user profile" in prefetched
        assert "fallback for project" in prefetched
        assert "indexed team memory" in prefetched
        assert "fallback for team" not in prefetched

    asyncio.run(run())
