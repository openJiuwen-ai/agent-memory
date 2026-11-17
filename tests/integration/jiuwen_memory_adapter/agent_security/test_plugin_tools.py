"""原生框架工具共用命名空间，重复装配不创建另一个执行对象。"""

import asyncio
import json

import pytest

from jiuwen_memory_adapter.agent_security import (
    EgressPolicy,
    SecurityDenied,
    SecurityPlugin,
    TargetRule,
)
from jiuwen_memory_adapter.agent_security.frameworks import AgentRunContext as _Run
from jiuwen_memory_adapter.agent_security.managed import ManagedAgent

from .fixtures import SAMPLE, sdk_security

pytestmark = pytest.mark.integration


@pytest.fixture(params=["openjiuwen"])
def assembled(request, sdk_source):
    framework = request.param
    pytest.importorskip("openjiuwen")
    runtime, _, _, _, actor, scope, _ = sdk_source
    plugin = SecurityPlugin(
        {"framework": framework, "memory_backend": "jiuwen"},
        policy=EgressPolicy([TargetRule("out")]),
        targets=[],
        authorize=lambda op: True,
        authorize_input=lambda *args: True,
        authorize_transfer=lambda *args: True,
    )
    ports = dict(
        api=runtime.api,
        security_for=lambda _: sdk_security(scope),
        scope_for=lambda _: scope,
        authorize_use=lambda *args: True,
        protected_source_levels_for=lambda _: frozenset({"S2", "S3"}),
    )
    try:
        backend = plugin.connect_memory_backend(provider="catalog", **ports)
        yield plugin, backend, actor
    finally:
        plugin.close()


def test_all_tool_types_share_one_namespace(assembled):
    plugin, backend, _ = assembled
    tools = plugin.memory_tools(backend)
    assert plugin.memory_tools(backend) is tools
    child = ManagedAgent(plugin, object(), "worker")
    for name in (backend.operations[0].name, "delegate", "send"):
        if name == "delegate":
            plugin.delegate_tool(child, name=name)
        elif name == "send":
            plugin.tool(
                name=name,
                target="out",
                description="send",
                input_schema={"type": "object"},
                send=lambda *args: None,
            )
        with pytest.raises(SecurityDenied, match="冲突"):
            plugin.delegate_tool(child, name=name)
        with pytest.raises(SecurityDenied, match="冲突"):
            plugin.tool(name=name, target="out")


def test_backend_tool_releases_only_safe_view(assembled):
    plugin, backend, actor = assembled
    tool = plugin.memory_tools(backend)[1]
    session = plugin.new_session(actor)
    token = plugin.enter_run(_Run(session, "test-read"))
    try:
        value = asyncio.run(tool.invoke({"limit": 1}))
        assert json.loads(value) and SAMPLE not in value
        assert plugin.sources.snapshot(session).level == "S2"
    finally:
        plugin.leave_run(token)
