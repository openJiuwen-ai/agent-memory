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
