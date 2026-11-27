# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""写入口的操作作用域与执行器身份（F10 决策 1、9）。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import assemble
from jiuwen_memory.common.errors import LLMTimeoutError
from jiuwen_memory.common.security.request_context import internal_context
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.executor import ExecutorLane
from tests.support.scoped_authenticator import ScopedAuthenticator

# 用例直接驱动内核私有入口（API/引擎/校验器），按仓库既有做法整文件豁免。
# pylint: disable=protected-access

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="", user="alice")


def _security():
    return internal_context(ScopedAuthenticator(Scope(org="acme", space="", user="alice")))


class _RecordingExecutor:
    """记录实际提交通道的执行器替身。"""

    def __init__(self) -> None:
        self.lanes: list[str] = []
        self.closed = False

    async def run(self, lane, fn, *args, **kwargs):
        self.lanes.append(lane.value)
        return await asyncio.to_thread(fn, *args, **kwargs)

    @staticmethod
    def current_lane():
        return None

    @staticmethod
    def health() -> None:
        return None

    def close(self, *, wait: bool = True) -> None:
        self.closed = True


class _PrivacyProbe:
    """只记录 staging 作用域进出的隐私替身。"""

    def __init__(self) -> None:
        self.depth = 0
        self.observed: dict[str, int] = {}

    @contextmanager
    def staging_scope(self, *, retain_on_success: bool = False):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    @staticmethod
    def preview(value, *, label=""):
        return value

    @staticmethod
    def preview_echo(value, *, label=""):
        return value


def test_privacy_scope_covers_route_auth_and_write(monkeypatch) -> None:
    api = assemble()
    probe = _PrivacyProbe()
    original_target = api._write_target
    original_auth = api._authorize
    original_write = api._commands.write

    def route(*args, **kwargs):
        probe.observed["route"] = probe.depth
        return original_target(*args, **kwargs)

    def authorize(*args, **kwargs):
        probe.observed["auth"] = probe.depth
        return original_auth(*args, **kwargs)

    async def write(*args, **kwargs):
        probe.observed["write"] = probe.depth
        return await original_write(*args, **kwargs)

    monkeypatch.setattr(api, "_privacy", probe)
    monkeypatch.setattr(api, "_write_target", route)
    monkeypatch.setattr(api, "_authorize", authorize)
    monkeypatch.setattr(api._commands, "write", write)
    try:
        asyncio.run(api.add_async("作用域覆盖", _SCOPE, security=_security()))
    finally:
        api.close()

    assert probe.observed == {"route": 1, "auth": 1, "write": 1}, (
        f"路由、鉴权、写入必须共享同一次操作的作用域，实际 {probe.observed}"
    )


def test_configured_executor_is_shared_with_engine() -> None:
    api = assemble()
    try:
        assert api._executor is not None, "装配必须注入具名执行器，不能靠 to_thread 兜底"
        assert api._executor is api._engine._executor, "API 与 Engine 必须使用同一实例"
    finally:
        api.close()


def test_orchestrator_shares_executor_and_signals_with_api() -> None:
    api = assemble()
    try:
        orchestrator = api._commands._orchestrator
        assert orchestrator._executor is api._executor, "编排器必须复用同一个执行器"
        assert orchestrator._signals is api._engine.write_signals, (
            "同一 Runtime 内引擎与编排器共写同一份降级计数"
        )
    finally:
        api.close()


def test_two_runtimes_do_not_share_degrade_counters() -> None:
    from jiuwen_memory.control.write_orchestrator_impl.signals import DegradeEvent

    first = assemble()
    second = assemble()
    try:
        first._engine.write_signals.record(DegradeEvent.MERGE_FALLBACK, batch_size=1)
        assert first._commands._orchestrator.degrade_counters() == {"merge_fallback": 1}
        assert second._commands._orchestrator.degrade_counters() == {}, (
            "计数器不得是进程级单例：另一个 Runtime 必须看不到"
        )
    finally:
        first.close()
        second.close()


def test_single_infer_timeout_releases_lock_without_repeating_extract(monkeypatch) -> None:
    api = assemble()
    timeout = LLMTimeoutError("model timed out")
    extract = Mock(side_effect=[timeout, []])
    try:
        monkeypatch.setattr(api._engine._evolver._extractor, "extract", extract)
        options = {"security": _security(), "system_metadata": {"infer": "true"}}
        with pytest.raises(LLMTimeoutError) as caught:
            api.add("第一轮", _SCOPE, **options)
        assert caught.value is timeout
        assert extract.call_count == 1
        assert "merge_fallback" not in api._engine.write_signals.counters()

        assert api.add("下一轮", _SCOPE, **options) == []
        assert extract.call_count == 2
    finally:
        api.close()


@pytest.mark.parametrize(
    ("count", "cause"), [(1, ValueError("invalid response")), (2, LLMTimeoutError("timeout"))],
)
def test_other_uncommitted_extract_failures_fall_back_per_item(monkeypatch, count, cause) -> None:
    from jiuwen_memory.control.types import BatchWriteItem

    api = assemble()
    extract = Mock(side_effect=[cause] + [[] for _ in range(count)])
    try:
        monkeypatch.setattr(api._engine._evolver._extractor, "extract", extract)
        items = [BatchWriteItem(content=f"条目{i}", scope=_SCOPE) for i in range(count)]
        result = api.batch_add(items, security=_security(), system_metadata={"infer": "true"})
        assert [outcome.error for outcome in result.outcomes] == [""] * count
        assert [len(call.args[0]) for call in extract.call_args_list] == [count] + [1] * count
        assert api._engine.write_signals.counters()["merge_fallback"] == 1
    finally:
        api.close()


def test_repeated_assembly_does_not_leak_threads() -> None:
    import threading

    baseline = threading.active_count()
    for _ in range(3):
        api = assemble()
        api.close()
    assert threading.active_count() <= baseline + 2, "重复装配/关闭不得遗留执行器线程"


def test_authorization_denied_before_engine_in_both_modes(monkeypatch) -> None:
    from jiuwen_memory.common.errors import PermissionDeniedError
    from jiuwen_memory.control.types import BatchWriteItem

    api = assemble()
    engine_calls = {"count": 0}
    original_write_group = api._engine.write_group

    async def counting_write_group(items, *, merge_extract):
        engine_calls["count"] += 1
        return await original_write_group(items, merge_extract=merge_extract)

    def deny(*args, **kwargs):
        raise PermissionDeniedError("denied")

    monkeypatch.setattr(api._engine, "write_group", counting_write_group)
    monkeypatch.setattr(api, "_authorize_and_check", lambda *a, **k: deny())
    items = [BatchWriteItem(content=f"条目{i}", scope=_SCOPE) for i in range(3)]
    try:
        merged = asyncio.run(api.batch_add_async(items, security=_security()))
        per_item = asyncio.run(
            api.batch_add_async(
                items,
                security=_security(),
                system_metadata={"merge_extract": "false"},
            )
        )
    finally:
        api.close()

    assert engine_calls["count"] == 0, "鉴权拒绝必须早于引擎写入（合并模式也不例外）"
    assert [outcome.error_type for outcome in merged.outcomes] == [
        "PermissionDeniedError"
    ] * 3
    assert [outcome.error_type for outcome in per_item.outcomes] == [
        "PermissionDeniedError"
    ] * 3


def test_single_and_batch_forward_same_item_fields(monkeypatch) -> None:
    """单条经过三层打包/解包，字段必须与批量入口逐一对齐。"""
    from datetime import UTC, datetime

    from jiuwen_memory.control.types import BatchWriteItem

    api = assemble()
    captured: list[BatchWriteItem] = []
    original = api._engine.write_group

    async def capture(items, *, merge_extract):
        captured.extend(items)
        return await original(items, merge_extract=merge_extract)

    monkeypatch.setattr(api._engine, "write_group", capture)
    occurred_at = datetime(2024, 1, 1, tzinfo=UTC)
    event_time = datetime(2024, 2, 1, tzinfo=UTC)
    fields = {
        "tags": ["tag"],
        "user_metadata": {"k": "v"},
        "occurred_at": occurred_at,
        "event_time": event_time,
    }
    try:
        asyncio.run(api.add_async("单条", _SCOPE, security=_security(), **fields))
        single = captured[-1]
        asyncio.run(
            api.batch_add_async(
                [BatchWriteItem(content="单条", scope=_SCOPE, **fields)],
                security=_security(),
            )
        )
        batch = captured[-1]
    finally:
        api.close()

    for name in (
        "content",
        "tags",
        "user_metadata",
        "occurred_at",
        "event_time",
        "source_ref",
        "assets",
    ):
        assert getattr(single, name) == getattr(batch, name), f"字段 {name} 未对齐"
    assert single.scope == batch.scope, "落点 scope 必须一致"


def test_route_uses_llm_lane_and_auth_and_audit_use_io_lane(monkeypatch) -> None:
    api = assemble()
    executor = _RecordingExecutor()
    monkeypatch.setattr(api, "_executor", executor)
    try:
        asyncio.run(api.add_async("通道分离", _SCOPE, security=_security()))
    finally:
        api.close()

    assert ExecutorLane.LLM.value in executor.lanes, "路由应走 LLM 泳道"
    io_calls = [lane for lane in executor.lanes if lane == ExecutorLane.IO.value]
    assert len(io_calls) >= 3, f"鉴权、摄入、审计应走 IO 泳道，实际 {executor.lanes}"
