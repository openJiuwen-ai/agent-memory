# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scene registration and coexistence through the real provider and document runtime."""
import asyncio
import json

import pytest

from jiuwen_memory_adapter.jiuwenswarm.coding_memory import TOOL_PREFIX, CodingMemoryScene
from jiuwen_memory_adapter.jiuwenswarm.memory_scenes import SCENE_FACTORIES
from tests.unit.jiuwen_memory_adapter.test_agent_memory_provider_unit import (
    AgentMemoryMemoryProvider,
    ApiScope,
    _security_provider,
)
from tests.unit.jiuwen_memory_adapter.test_agent_memory_provider_unit import (
    _offline_in_process_config as _offline_in_process_config,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("close_first", ["agent", "code"])
def test_sdk_work_code_coexist_and_close_independently(tmp_path, close_first):
    async def run():
        providers = {scene: AgentMemoryMemoryProvider(
            scene=scene, project="code-project", portal={"enabled": False},
            security_provider=_security_provider(ApiScope(org="org", user="user")),
        ) for scene in ("agent", "code")}

        async def write(scene, content):
            tool, args = ("agent_memory_conclude", {
                "conclusion": content, "project": "work-project",
            }) if scene == "agent" else (TOOL_PREFIX + "write", {
                "content": content, "project": "work-project",
            })
            return json.loads(await providers[scene].handle_tool_call(tool, args))

        try:
            await asyncio.gather(*(provider.initialize(
                user_id="user", scope_id="org", agent_id="same-agent", session_id="",
            ) for provider in providers.values()))
            results = await asyncio.gather(write("agent", "Work fact"), write("code", "Code fact"))
            assert all(result.get("item_id") for result in results)
            assert "Work fact" in (tmp_path / "memory/work-project/MEMORY.md").read_text()
            assert "Code fact" in (tmp_path / "memory/code-project/MEMORY.md").read_text()
            work = await providers["agent"].prefetch("fact", project="work-project")
            code = await providers["code"].prefetch("fact", project="work-project")
            assert "Work fact" in work and "Code fact" not in work
            assert "Code fact" in code and "Work fact" not in code

            await providers[close_first].shutdown()
            survivor = "code" if close_first == "agent" else "agent"
            assert (await write(survivor, "survivor fact")).get("item_id")
            assert "survivor fact" in await providers[survivor].prefetch(
                "survivor", project="work-project",
            )
        finally:
            await asyncio.gather(*(provider.shutdown() for provider in providers.values()))

    asyncio.run(run())


def test_registered_scene_uses_existing_provider_pipeline(tmp_path, monkeypatch):
    monkeypatch.setitem(
        SCENE_FACTORIES, "review", lambda project: CodingMemoryScene("review-" + project),
    )

    async def run():
        provider = AgentMemoryMemoryProvider(
            scene="review", project="repo", portal={"enabled": False},
            security_provider=_security_provider(ApiScope(org="org", user="user")),
        )
        try:
            await provider.initialize(
                user_id="user", scope_id="org", agent_id="reviewer", session_id="",
            )
            assert len(provider.get_tool_schemas()) == 4
            assert "Coding memory" in provider.system_prompt_block()
            result = json.loads(await provider.handle_tool_call(
                TOOL_PREFIX + "write", {"content": "review fact"},
            ))
            assert result["stored"] is True
            assert "review fact" in await provider.prefetch("review")
            await provider.sync_turn("review request", "review completed", project="ignored")
            assert "review completed" in (tmp_path / "memory/review-repo/MEMORY.md").read_text()
            assert not (tmp_path / "memory/ignored").exists()
        finally:
            await provider.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("scene", ["agent", "code"])
def test_scene_tool_schemas_are_not_shared(scene):
    first = AgentMemoryMemoryProvider(scene=scene)
    schemas = first.get_tool_schemas()
    original = schemas[0]["name"]
    schemas[0]["name"] = "mutated"
    schemas[0]["parameters"]["properties"].clear()
    second = AgentMemoryMemoryProvider(scene=scene)
    assert first.get_tool_schemas()[0]["name"] == original
    assert second.get_tool_schemas()[0]["parameters"]["properties"]


def test_unknown_scene_does_not_use_work_defaults():
    with pytest.raises(ValueError, match="Unknown memory scene"):
        AgentMemoryMemoryProvider(scene="unknown")


@pytest.mark.parametrize("background", [False, True])
def test_coding_scene_prefetch_excludes_shared_credentials(background):
    async def run():
        provider = AgentMemoryMemoryProvider(
            scene="code",
            project="repo",
            portal={"enabled": False},
            security_provider=_security_provider(ApiScope(org="org", user="user")),
        )
        await provider.initialize(
            user_id="user", scope_id="org", agent_id="coder", session_id="",
        )
        try:
            secret = "用户的邮箱口令是 coding-secret-9981。"
            pg_conn = "数据库 postgresql://alice:pg-secret@db.example.com:5432/app"
            email = "用户的邮箱是 dev@example.com。"
            author_url = "团队文章索引 https://docs.example.com/articles?author=alice"
            project_fact = "本项目使用 pytest 运行单元测试。"

            async def _read(md_filename, scope, *, project="", line_range=None):
                if md_filename == "USER.md":
                    return [
                        {"item_id": "c1", "content": secret, "is_credential": True},
                        {"item_id": "e1", "content": email, "is_credential": False},
                        {"item_id": "a1", "content": author_url, "is_credential": False},
                    ]
                return [
                    {"item_id": "c2", "content": pg_conn, "is_credential": True},
                    {"item_id": "p1", "content": project_fact, "is_credential": False},
                ]

            seen_filters: list[dict | None] = []

            async def _search(query, scope, *, top_k=5, filters=None):
                seen_filters.append(filters)
                # 即使检索结果只返回被标记为凭据的条目，也应先过滤再回退到 MEMORY.md 非凭据条目
                return [{"item_id": "c2", "content": pg_conn, "is_credential": True}]

            client = getattr(provider, "_client")
            setattr(client, "read_md_file", _read)
            setattr(client, "search", _search)
            text = await provider.prefetch("帮我修一个函数", background=background)
            if not background:
                assert seen_filters and seen_filters[0]["system_metadata.is_credential"] == {
                    "ne": "true"
                }, f"Coding 预取检索应携带凭据排除过滤: {seen_filters}"
            assert "coding-secret-9981" not in text, "Coding 预取不得注入标记为凭据的 USER.md 条目"
            assert "pg-secret" not in text, "Coding 预取不得注入标记为凭据的项目数据库连接串"
            assert email in text
            assert author_url in text, "带 ?author=alice 的链接不应被误删"
            assert project_fact in text, "搜索命中全部被过滤时应回退到 MEMORY.md 非凭据条目"
        finally:
            await provider.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("is_credential", [False, True])
def test_coding_write_project_fact_false_remains_searchable(tmp_path, monkeypatch, is_credential):
    """Coding 工具写入 project_fact=False（含凭据）仍保留绑定项目：显式搜索可查回、凭据不进预取。"""
    import re

    import agent_memory_provider as adapter

    from jiuwen_memory.construction.extractor_impl.agent_llm_extractor import AgentLlmExtractor

    offline_config = getattr(adapter, "_agent_default_config")

    def config():
        result = offline_config()
        result["extractor"]["document_fact"] = {"target": "document_fact"}
        return result

    fact = (
        "发布服务 API Token 是 tok-coding-8899。"
        if is_credential
        else "发布前统一先执行数据库迁移脚本。"
    )

    def extract(_self, messages):
        source_id = re.search(r"\[ID: ([^\]]+)\]", messages[1].content).group(1)
        return json.dumps([
            {
                "source_id": source_id,
                "content": fact,
                "memory_class": "long_term_memory",
                "project_fact": False,
                "is_credential": is_credential,
                "tier": "semantic",
                "confidence": 1.0,
            }
        ])

    monkeypatch.setattr(adapter, "_agent_default_config", config)
    monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", extract)

    async def run():
        provider = AgentMemoryMemoryProvider(
            scene="code",
            project="repo-coding",
            portal={"enabled": False},
            security_provider=_security_provider(ApiScope(org="org", user="user")),
        )
        await provider.initialize(
            user_id="user", scope_id="org", agent_id="coder", session_id="",
        )
        try:
            write_res = json.loads(await provider.handle_tool_call(
                TOOL_PREFIX + "write", {"content": fact},
            ))
            assert write_res["stored"] is True
            assert fact in (tmp_path / "memory" / "repo-coding" / "MEMORY.md").read_text(
                encoding="utf-8"
            )
            assert not (tmp_path / "memory" / "MEMORY.md").exists()
            search_res = json.loads(await provider.handle_tool_call(
                TOOL_PREFIX + "search", {"query": fact},
            ))
            assert any(fact in it["memory"] for it in search_res["results"])
            prefetched = await provider.prefetch(fact)
            if is_credential:
                assert "tok-coding-8899" not in prefetched, "Coding 预取不得注入凭据"
            else:
                assert fact in prefetched
        finally:
            await provider.shutdown()

    asyncio.run(run())
