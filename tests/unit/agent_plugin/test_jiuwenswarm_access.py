"""JiuwenSwarm provider：五维 Scope，list 走 MemoryAPI，不直读 KV。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]

_DEFAULT_ENGINE_PARAMS = {
    "ingestor": "default",
    "index_builder": "default",
    "retriever": "default",
    "kv_store": "default",
    "scheduler": "default",
    "evolver": "default",
    "lifecycle": "default",
}


def _install_openjiuwen_stub() -> None:
    names = (
        "openjiuwen",
        "openjiuwen.core",
        "openjiuwen.core.memory",
        "openjiuwen.core.memory.external",
        "openjiuwen.core.memory.external.provider",
    )
    for name in names:
        sys.modules.setdefault(name, ModuleType(name))
    sys.modules["openjiuwen.core.memory.external.provider"].MemoryProvider = object


def _load_provider():
    _install_openjiuwen_stub()
    plugin_dir = str(_REPO / "jiuwen_memory_adapter" / "jiuwenswarm")
    if plugin_dir not in sys.path:
        sys.path.append(plugin_dir)
    import agent_memory_provider as provider

    return provider


def _write_in_process_config() -> str:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(
            {"engine": {"default": {"target": "cloud", "params": _DEFAULT_ENGINE_PARAMS}}},
            handle,
        )
        return handle.name


def test_scope_keeps_explicit_space_and_compat_scope_id_as_org() -> None:
    provider = _load_provider()
    adapter = provider.AgentMemoryMemoryProvider(
        user_id="alice",
        agent_id="bot",
        space="product",
    )
    asyncio.run(
        adapter.initialize(scope_id="acme", session_id="sess-1", space="product")
    )
    scope = adapter.bound_scope()
    assert scope.org == "acme"
    assert scope.space == "product"
    assert scope.user == "alice"
    assert scope.agent == "bot"
    assert scope.session == "sess-1"


def test_scope_does_not_guess_space_from_scope_id() -> None:
    provider = _load_provider()
    adapter = provider.AgentMemoryMemoryProvider(user_id="alice")
    asyncio.run(adapter.initialize(scope_id="acme"))
    scope = adapter.bound_scope()
    assert scope.org == "acme"
    assert scope.space == ""


def test_in_process_list_uses_memory_api_and_isolates_space() -> None:
    provider = _load_provider()
    adapter = provider.AgentMemoryMemoryProvider(
        user_id="owner",
        config_path=_write_in_process_config(),
    )
    asyncio.run(adapter.initialize(scope_id="acme", space="alpha"))
    asyncio.run(
        adapter.handle_tool_call(
            "agent_memory_conclude", {"conclusion": "alpha-memory"}
        )
    )
    asyncio.run(adapter.initialize(scope_id="acme", space="beta"))
    asyncio.run(
        adapter.handle_tool_call(
            "agent_memory_conclude", {"conclusion": "beta-memory"}
        )
    )
    asyncio.run(adapter.initialize(scope_id="acme", space="alpha"))
    listed = json.loads(
        asyncio.run(adapter.handle_tool_call("agent_memory_profile", {}))
    )
    result = listed.get("result", "")
    assert "alpha-memory" in result
    assert "beta-memory" not in result

    source = (
        _REPO / "jiuwen_memory_adapter" / "jiuwenswarm" / "agent_memory_provider.py"
    ).read_text(encoding="utf-8")
    assert "self._kv" not in source
    assert "kernel.kv" not in source

    scope = adapter.bound_scope()
    assert scope.org == "acme"
    assert scope.space == "alpha"
    assert scope.user == "owner"
