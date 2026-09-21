"""JiuwenSwarm provider：五维 Scope，list 走 MemoryAPI，不直读 KV。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

from jiuwen_memory.api import Scope as ApiScope
from jiuwen_memory.api import Surface
from jiuwen_memory.common.security import new_request_context
from jiuwen_memory.common.security.types import AuthContext, Role

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


def _security_provider(actor: ApiScope, *, role: Role = Role.USER):
    """测试用可信 security provider：模拟已完成认证的 composition root。

    P1-2 起进程内模式不从业务 scope 自述身份——actor 只能由注入的可信 provider
    产出（生产等价物是 API Key / Trusted Gateway 的认证结论）。返回的 callable
    每次调用给出同一份已认证上下文，与业务 target 无关。
    """
    context = new_request_context(
        AuthContext(
            actor=actor,
            role=role,
            credential_type="internal",
            auth_method="internal",
            authenticated_at=datetime.now(timezone.utc),
        ),
        surface=Surface.INTERNAL,
    )
    return lambda: context


def test_scope_keeps_explicit_space_and_compat_scope_id_as_org() -> None:
    provider = _load_provider()
    adapter = provider.AgentMemoryMemoryProvider(
        user_id="alice",
        agent_id="bot",
        space="product",
        security_provider=_security_provider(ApiScope(org="acme", user="alice")),
    )
    asyncio.run(adapter.initialize(scope_id="acme", session_id="sess-1", space="product"))
    scope = adapter.bound_scope()
    assert scope.org == "acme"
    assert scope.space == "product"
    assert scope.user == "alice"
    assert scope.agent == "bot"
    assert scope.session == "sess-1"


def test_scope_does_not_guess_space_from_scope_id() -> None:
    provider = _load_provider()
    adapter = provider.AgentMemoryMemoryProvider(
        user_id="alice",
        security_provider=_security_provider(ApiScope(org="acme", user="alice")),
    )
    asyncio.run(adapter.initialize(scope_id="acme"))
    scope = adapter.bound_scope()
    assert scope.org == "acme"
    assert scope.space == ""


def test_in_process_list_uses_memory_api_and_isolates_space() -> None:
    provider = _load_provider()
    # 身份是固定的已认证 principal（P1-2）：注入后不再随业务 scope 变化。固定 actor
    # 无法 owner-cover 两个 space，故用 ROOT 档放行写入——本测试钉的是**查询层**
    # 按 scope 过滤（alpha 看不到 beta），授权档位不是这里的主题。
    adapter = provider.AgentMemoryMemoryProvider(
        user_id="owner",
        config_path=_write_in_process_config(),
        security_provider=_security_provider(ApiScope(org="acme", user="owner"), role=Role.ROOT),
    )
    asyncio.run(adapter.initialize(scope_id="acme", space="alpha"))
    asyncio.run(adapter.handle_tool_call("agent_memory_conclude", {"conclusion": "alpha-memory"}))
    asyncio.run(adapter.initialize(scope_id="acme", space="beta"))
    asyncio.run(adapter.handle_tool_call("agent_memory_conclude", {"conclusion": "beta-memory"}))
    asyncio.run(adapter.initialize(scope_id="acme", space="alpha"))
    listed = json.loads(asyncio.run(adapter.handle_tool_call("agent_memory_profile", {})))
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


def test_in_process_target_change_does_not_change_authenticated_actor() -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    """P1-2 回归：业务 target 变化不得改变已认证 actor。

    ``_InProcessClient`` 每次调用都从注入的 security provider 取身份，业务传入的
    api_scope 只作 target。用录制 API 记录每次调用实际收到的 (target, actor)，
    断言 actor 恒为注入的那一个——包括 target 换成别的 org/user 也一样。
    """
    provider = _load_provider()
    actor = ApiScope(org="acme", user="owner")
    security_provider = _security_provider(actor)

    client = provider._InProcessClient(None, security_provider=security_provider)

    recorded = []

    class _RecordingApi:
        async def add_async(self, _content, scope, *, source, security, **_kwargs):
            recorded.append((scope, security.auth.actor))
            return []

    client._api = _RecordingApi()

    targets = [
        ApiScope(org="acme", space="alpha", user="owner"),
        ApiScope(org="acme", space="beta", user="owner"),
        ApiScope(org="other", user="mallory"),  # 冒充他人 target 也不改 actor
    ]
    for target in targets:
        asyncio.run(client.add("m", target))

    assert [seen for _, seen in recorded] == [actor] * len(targets)
    assert [target for target, _ in recorded] == targets
