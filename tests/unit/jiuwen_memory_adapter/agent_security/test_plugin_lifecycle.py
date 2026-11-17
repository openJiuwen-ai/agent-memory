"""装配失败与关闭异常不能留下可调用资源或冲突的工具定义。"""

import asyncio
from types import SimpleNamespace

import pytest

from jiuwen_memory_adapter.agent_security import (
    EgressPolicy,
    SecurityDenied,
    SecurityPlugin,
    TargetRule,
)

from .fixtures import ACTOR

pytestmark = pytest.mark.unit


@pytest.fixture
def plugin():
    instance = SecurityPlugin(
        {"framework": "openjiuwen"},
        policy=EgressPolicy([TargetRule("out")]),
        targets=[],
        authorize=lambda op: True,
        authorize_input=lambda *args: True,
        authorize_transfer=lambda *args: True,
    )
    yield instance
    instance.close()


def test_close_attempts_all_resources_even_when_several_fail(plugin):
    calls = []
    session = plugin.new_session(ACTOR)

    def close(name, fail=False):
        calls.append(name)
        if fail:
            raise RuntimeError(name)

    async def aclose():
        close("async-client")

    plugin.runtime_context.own_client(
        SimpleNamespace(close=lambda: close("bad-client", True)), False
    )
    plugin.runtime_context.own_client(SimpleNamespace(aclose=aclose), True)
    # 白盒注入多个独立关闭故障，断言失败不跳过后续资源。
    plugin._memory_backends.extend(  # pylint: disable=protected-access
        [
            SimpleNamespace(close=lambda: close("bad-backend", True)),
            SimpleNamespace(close=lambda: close("last-backend")),
        ]
    )
    with pytest.raises(ExceptionGroup) as caught:
        plugin.close()
    assert len(caught.value.exceptions) == 2
    assert calls == ["bad-client", "async-client", "bad-backend", "last-backend"]
    with pytest.raises(SecurityDenied):
        plugin.sources.snapshot(session)
    plugin.close()
    assert len(calls) == 4, "重复关闭不得再次调用已经处理过的资源"


def test_cancelled_close_still_cleans_other_resources(plugin):
    calls = []

    async def cancelled():
        raise asyncio.CancelledError()

    plugin.runtime_context.own_client(SimpleNamespace(aclose=cancelled), True)
    plugin.runtime_context.own_client(SimpleNamespace(close=lambda: calls.append("last")), False)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(plugin.aclose())
    assert calls == ["last"]
    with pytest.raises(SecurityDenied):
        plugin.new_session(ACTOR)


def test_closed_plugin_cannot_create_new_resources(plugin):
    plugin.close()
    with pytest.raises(SecurityDenied):
        plugin.connect_memory_backend(api=object())
    with pytest.raises(SecurityDenied):
        plugin.tool(
            name="send", description="send", input_schema={}, target="out", send=lambda *a: None
        )


def test_failed_tool_creation_does_not_reserve_plugin_target(plugin, monkeypatch):
    def fail(**definition):
        raise ValueError("invalid tool schema")

    monkeypatch.setattr(plugin.execution, "create_tool", fail)
    with pytest.raises(ValueError):
        plugin.tool(name="send", target="out")
    assert "send" not in plugin.coverage().tools
