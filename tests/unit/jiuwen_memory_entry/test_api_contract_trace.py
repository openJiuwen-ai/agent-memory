# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""invoke_api 的 trace 作用域与响应填充契约测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.request_context import internal_context
from jiuwen_memory.common.trace import TraceLevel, configure_default, resolve_target, trace_event
from jiuwen_memory.common.type_def import MemoryUnit, Modality, Scope
from jiuwen_memory_entry.core import api_contract
from tests.support.scoped_authenticator import ScopedAuthenticator

register_plugins()


class _Stub:
    """无埋点 stub。"""

    def check_write(self, scope, security=None):
        return {"ok": True}


class _TracingStub:
    """埋点 stub：调用中记录一条 trace。"""

    def check_write(self, scope, security=None):
        trace_event("TracingStub", "write", {"n": 1})
        return {"ok": True}

    def trace(self, unit_id=None, security=None, **kwargs):
        trace_event("TracingStub", "write", {"n": 1})
        return {"unit_id": unit_id}


@pytest.fixture(autouse=True)
def _memory_default():
    configure_default(resolve_target("memory"))
    yield
    configure_default(resolve_target("noop"))


@pytest.fixture()
def _contract():
    with (
        patch.object(api_contract, "parse_request", return_value={"scope": {}}),
        patch.object(api_contract, "method_contract") as mc,
    ):
        mc.return_value.is_async = False
        yield


@pytest.mark.unit
def test_empty_snapshot_adds_no_key(_contract) -> None:
    out = api_contract.invoke_api(_Stub(), "check_write", {"scope": {}}, None)
    assert "trace" not in out, "无事件时不得添加 trace 键"


@pytest.mark.unit
def test_collected_trace_fills_response(_contract) -> None:
    out = api_contract.invoke_api(_TracingStub(), "check_write", {"scope": {}}, None)
    assert out["ok"] is True
    assert out["trace"]["spans"] == [
        {"component": "TracingStub", "stage": "write", "data": {"n": 1}}
    ], "收集到的 trace 应填充进响应"
    assert out["trace"]["trace_id"], "响应 trace 应带 trace_id"


@pytest.mark.unit
def test_trace_verb_not_augmented(_contract) -> None:
    out = api_contract.invoke_api(_TracingStub(), "trace", {"scope": {}}, None)
    assert "trace" not in out, "trace 动词（血缘回溯）不得叠加处理过程 trace"


@pytest.mark.unit
def test_request_override_to_noop(_contract) -> None:
    out = api_contract.invoke_api(
        _TracingStub(), "check_write", {"scope": {}, "extensions": {"trace": "noop"}}, None
    )
    assert "trace" not in out, "请求级覆盖 noop 应关闭本次收集"


@pytest.mark.unit
def test_request_override_rejects_non_string(_contract) -> None:
    with pytest.raises(ValidationError, match="trace"):
        api_contract.invoke_api(
            _TracingStub(), "check_write", {"scope": {}, "extensions": {"trace": 1}}, None
        )


@pytest.mark.unit
def test_scope_closed_after_exception(_contract) -> None:
    class _Failing:
        def check_write(self, scope, security=None):
            trace_event("Failing", "write", {"n": 1})
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom") as raised:
        api_contract.invoke_api(_Failing(), "check_write", {"scope": {}}, None)
    error_trace = api_contract.error_trace(raised.value)
    assert error_trace is not None and error_trace["spans"][0]["component"] == "Failing", (
        "失败请求的 trace 应挂在异常上供 surface 放进错误体"
    )
    # 作用域必须在异常路径也关闭：下一条无事件请求不得带出上一条的 trace
    out = api_contract.invoke_api(_Stub(), "check_write", {"scope": {}}, None)
    assert "trace" not in out, "异常路径未关闭作用域会泄漏上一请求的 trace"


class _AddApi:
    """真实签名的 add stub：不 mock parse_request，覆盖真实参数校验链路。"""

    @staticmethod
    def add(content, scope, source=Modality.TEXT, *, security, **_kwargs):
        trace_event("AddApi", "write", {"chars": len(content)})
        return [MemoryUnit(id="u1", scope=scope)]


_ADD_RAW = {"content": "remember", "scope": {"org": "acme", "user": "alice"}}


@pytest.mark.unit
def test_override_stripped_before_parse_request() -> None:
    raw = {**_ADD_RAW, "extensions": {"trace": "memory"}}
    out = api_contract.invoke_api(_AddApi(), "add", raw, object())
    assert out["trace"]["spans"][0]["component"] == "AddApi", "覆盖键应在签名校验前剥离"
    assert raw["extensions"] == {"trace": "memory"}, "不得修改调用方 payload"


@pytest.mark.unit
def test_other_extensions_still_validated() -> None:
    raw = {**_ADD_RAW, "extensions": {"trace": "memory", "other": 1}}
    with pytest.raises(ValidationError, match="extensions"):
        api_contract.invoke_api(_AddApi(), "add", raw, object())


@pytest.mark.unit
def test_list_result_wrapped_only_when_traced() -> None:
    out = api_contract.invoke_api(_AddApi(), "add", _ADD_RAW, object())
    assert set(out) == {"result", "trace"}, "list 结果有 trace 时包成 result + trace"
    assert out["result"][0]["id"] == "u1"
    plain = api_contract.invoke_api(
        _AddApi(), "add", {**_ADD_RAW, "extensions": {"trace": "noop"}}, object()
    )
    assert isinstance(plain, list) and plain[0]["id"] == "u1", "无 trace 时返回结构不变"


@pytest.mark.unit
def test_legacy_dispatch_strips_override() -> None:
    from jiuwen_memory_entry.core import handler
    from jiuwen_memory_entry.core.dispatch_request import DispatchRequest

    seen: list[dict] = []

    def _route(srv, request):
        seen.append(dict(request.payload))
        trace_event("Legacy", "route", {})
        return {"ok": True}

    request = DispatchRequest(
        security=internal_context(ScopedAuthenticator(Scope(org="o", user="u"))),
        verb="health",
        actor=Scope(org="o", user="u"),
        target=None,
        payload={"extensions": {"trace": "memory"}},
    )
    with patch.dict(handler._ROUTES, {"health": _route}):
        status, body = handler.dispatch(None, request)
    assert status == 200 and seen == [{}], "legacy 通道同样剥离覆盖键"
    assert body["trace"]["spans"][0]["component"] == "Legacy"


class _DetailApi:
    @staticmethod
    def add(content, scope, source=Modality.TEXT, *, security, **_kwargs):
        trace_event("AddApi", "detail", {"n": 1}, level=TraceLevel.DEBUG)
        return [MemoryUnit(id="u1", scope=scope)]


@pytest.mark.unit
def test_override_level_enables_debug_detail() -> None:
    out = api_contract.invoke_api(_DetailApi(), "add", _ADD_RAW, object())
    assert isinstance(out, list), "默认 info 阈值下仅有 debug 埋点时不附 trace"
    raw = {**_ADD_RAW, "extensions": {"trace": ":debug"}}
    out = api_contract.invoke_api(_DetailApi(), "add", raw, object())
    assert out["trace"]["spans"][0]["level"] == "debug", "':debug' 应沿用默认实现并开明细"


@pytest.mark.unit
def test_override_rejects_unknown_level() -> None:
    raw = {**_ADD_RAW, "extensions": {"trace": "memory:fatal"}}
    with pytest.raises(ValidationError, match="trace level"):
        api_contract.invoke_api(_AddApi(), "add", raw, object())


@pytest.mark.unit
def test_legacy_dispatch_unknown_target_is_400() -> None:
    from jiuwen_memory_entry.core import handler
    from jiuwen_memory_entry.core.dispatch_request import DispatchRequest

    request = DispatchRequest(
        security=internal_context(ScopedAuthenticator(Scope(org="o", user="u"))),
        verb="health",
        actor=Scope(org="o", user="u"),
        target=None,
        payload={"extensions": {"trace": "nope"}},
    )
    with patch.dict(handler._ROUTES, {"health": lambda srv, req: {"ok": True}}):
        status, body = handler.dispatch(None, request)
    assert status == 400 and body["error"] == "ValidationError", body
