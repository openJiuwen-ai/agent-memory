# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""common/trace 横切组件契约测试：4 种实现、调用树、请求作用域隔离、体积护栏、覆盖键。"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from contextlib import contextmanager

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.request_context import reset_request_id, set_request_id
from jiuwen_memory.common.trace import (
    TraceLevel,
    TraceProducer,
    begin_trace,
    configure_default,
    end_trace,
    parse_trace_spec,
    resolve_target,
    trace_annotate,
    trace_event,
    trace_span,
    traced,
)
from jiuwen_memory.common.trace import trace as trace_mod

register_plugins()


@pytest.fixture(autouse=True)
def _reset_default():
    """每个用例后恢复 noop 默认实现，避免用例间泄漏。"""
    yield
    configure_default(resolve_target("noop"))


@contextmanager
def _capture_logger(name: str):
    """挂一个收集 handler 到指定 logger（agent_memory 根 propagate=False，caplog 抓不到）。

    未调用 setup_logging 时有效级别为 WARNING，临时降到 DEBUG，退出时恢复。
    """
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger(name)
    handler = _Collect()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _spans(snapshot: dict) -> list[dict]:
    return snapshot["spans"]


@pytest.mark.unit
class TestTraceScope:
    def test_event_without_scope_is_silent(self) -> None:
        trace_event("Comp", "stage", {"a": 1})  # 不抛错即契约

    def test_noop_collects_nothing(self) -> None:
        token = begin_trace("noop")
        trace_event("Comp", "stage", {"a": 1})
        assert end_trace(token) == {}, "noop 不得收集任何数据"

    def test_noop_scope_short_circuits(self) -> None:
        token = begin_trace("noop")
        try:
            assert trace_mod._CURRENT.get() is None, "noop 作用域应置空，埋点与无作用域同价"
        finally:
            end_trace(token)

    def test_noop_masks_outer_scope(self) -> None:
        outer = begin_trace("memory")
        inner = begin_trace("noop")
        trace_event("Comp", "stage", {"a": 1})
        end_trace(inner)
        assert end_trace(outer) == {}, "内层 noop 期间的事件不得落进外层作用域"

    def test_memory_collects_events_in_order(self) -> None:
        token = begin_trace("memory", trace_id="t-1")
        trace_event("PipelineRetriever", "recall", {"channel": "vector"})
        trace_event("PipelineRetriever", "recall", {"channel": "bm25"})
        snapshot = end_trace(token)
        assert snapshot == {
            "trace_id": "t-1",
            "spans": [
                {
                    "component": "PipelineRetriever",
                    "stage": "recall",
                    "data": {"channel": "vector"},
                },
                {"component": "PipelineRetriever", "stage": "recall", "data": {"channel": "bm25"}},
            ],
        }, "同名事件应按发生顺序逐条保留，不覆盖"

    def test_span_nests_children_and_times(self) -> None:
        token = begin_trace("memory")
        with trace_span("Engine", "write"):
            trace_event("Engine", "ingest", {"units": 1})
            with trace_span("Evolver", "evolve"):
                trace_event("Extractor", "extract", {"items": 2})
                trace_annotate({"created": 2})
        (root,) = _spans(end_trace(token))
        assert root["component"] == "Engine" and root["stage"] == "write", root
        assert root["cost_ms"] >= 0, "span 应记录耗时"
        ingest, evolve = root["children"]
        assert ingest == {"component": "Engine", "stage": "ingest", "data": {"units": 1}}
        assert evolve["data"] == {"created": 2}, "trace_annotate 应合并进当前 span"
        assert evolve["children"] == [
            {"component": "Extractor", "stage": "extract", "data": {"items": 2}}
        ], "span 内事件应挂为其 children"

    def test_span_records_error_type(self) -> None:
        token = begin_trace("memory")
        with pytest.raises(RuntimeError):
            with trace_span("Engine", "write"):
                raise RuntimeError("secret body")
        (root,) = _spans(end_trace(token))
        assert root["error"] == "RuntimeError", "span 异常只记类型名"
        assert "secret" not in str(root), "不得记录异常文本"

    def test_traced_decorator_sync_and_async(self) -> None:
        class Engine:
            @traced("write")
            async def write(self) -> int:
                trace_event(type(self).__name__, "ingest", {"units": 1})
                return 1

            @traced("evolve", component="Custom")
            def evolve(self) -> int:
                return 2

        token = begin_trace("memory")
        assert asyncio.run(Engine().write()) == 1
        assert Engine().evolve() == 2
        write, evolve = _spans(end_trace(token))
        assert (write["component"], write["stage"]) == ("Engine", "write"), write
        assert write["children"][0]["stage"] == "ingest", "协程内事件应挂到方法 span"
        assert (evolve["component"], evolve["stage"]) == ("Custom", "evolve"), evolve

    def test_concurrent_tasks_attach_to_launching_span(self) -> None:
        async def channel(name: str) -> None:
            with trace_span("Recaller", name):
                await asyncio.sleep(0)
                trace_event("Recaller", "hit", {"channel": name})

        async def run() -> None:
            with trace_span("Retriever", "retrieve"):
                await asyncio.gather(channel("vector"), channel("bm25"))

        token = begin_trace("memory")
        asyncio.run(run())
        (root,) = _spans(end_trace(token))
        stages = [child["stage"] for child in root["children"]]
        assert sorted(stages) == ["bm25", "vector"], "并发子任务应平级挂在发起 span 下"
        for child in root["children"]:
            assert child["children"][0]["data"] == {"channel": child["stage"]}, child

    def test_to_thread_inherits_span(self) -> None:
        async def run() -> None:
            with trace_span("Engine", "write"):
                await asyncio.to_thread(trace_event, "Evolver", "dedup", {"add": 1})

        token = begin_trace("memory")
        asyncio.run(run())
        (root,) = _spans(end_trace(token))
        assert root["children"][0]["stage"] == "dedup", "to_thread 应继承当前 span"

    def test_log_writes_log_with_trace_id_and_path(self) -> None:
        with _capture_logger("agent_memory.common.trace.trace_impl.log_trace") as records:
            token = begin_trace("log", trace_id="rid-9")
            with trace_span("Engine", "write"):
                trace_event("Builder", "build", {"units": 3})
            assert end_trace(token) == {}, "log 实现不留调用树"
        assert any("rid-9" in r and "info Engine.write/Builder.build" in r for r in records), (
            f"日志应带 trace_id 与层级 path: {records}"
        )
        assert any("Engine.write" in r and "cost_ms" in r for r in records), "span 退出应记耗时"

    def test_both_collects_and_logs(self) -> None:
        with _capture_logger("agent_memory.common.trace.trace_impl.log_trace") as records:
            token = begin_trace("both")
            trace_event("Comp", "stage", {"k": "v"})
            snapshot = end_trace(token)
        assert _spans(snapshot) == [{"component": "Comp", "stage": "stage", "data": {"k": "v"}}]
        assert any("Comp.stage" in r for r in records), "both 应写日志"

    def test_trace_id_defaults_to_request_id(self) -> None:
        rid = set_request_id("req-42")
        try:
            token = begin_trace("memory")
            trace_event("C", "s", {})
            assert end_trace(token)["trace_id"] == "req-42", "trace_id 应复用认证请求的 request_id"
        finally:
            reset_request_id(rid)

    def test_concurrent_requests_are_isolated(self) -> None:
        results: dict[int, dict] = {}

        def worker(n: int) -> None:
            token = begin_trace("memory", trace_id=f"t{n}")
            trace_event(f"w{n}", "s", {"n": n})
            results[n] = end_trace(token)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(
            results[i]
            == {
                "trace_id": f"t{i}",
                "spans": [{"component": f"w{i}", "stage": "s", "data": {"n": i}}],
            }
            for i in range(8)
        ), "并发请求 trace 不得互相串数据"

    def test_end_trace_resets_scope(self) -> None:
        token = begin_trace("memory")
        end_trace(token)
        trace_event("C", "s", {"v": 1})
        token2 = begin_trace("memory")
        assert end_trace(token2) == {}, "end 后作用域必须关闭，后续事件不得进入上一请求"

    def test_context_captured_before_end_drops_late_events(self) -> None:
        # 模拟调度器后台任务：请求期间复制 context，请求结束后才在其中记录事件。
        token = begin_trace("both")
        with trace_span("Engine", "write"):
            captured = contextvars.copy_context()
        request = trace_mod._CURRENT.get()
        end_trace(token)
        with _capture_logger("agent_memory.common.trace.trace_impl.log_trace") as records:
            captured.run(trace_event, "Evolver", "consolidate", {"v": 1})
        assert request is not None and len(request.roots) == 1, "已结束请求的调用树不得再被写入"
        assert "children" not in request.roots[0].to_dict(), "后台事件不得挂到旧 span"
        assert records == [], "已结束请求的作用域不得再写日志"

    def test_stale_span_not_reused_by_next_request(self) -> None:
        first = begin_trace("memory")
        with trace_span("Engine", "write"):
            captured = contextvars.copy_context()
        end_trace(first)

        def next_request() -> dict:
            token = begin_trace("memory")
            trace_event("C", "s", {})
            return end_trace(token)

        snapshot = captured.run(next_request)
        assert _spans(snapshot) == [{"component": "C", "stage": "s"}], (
            "复制来的旧 span 不得成为新请求的父节点"
        )


_DEBUG = TraceLevel.DEBUG


@pytest.mark.unit
class TestLevel:
    def test_debug_event_dropped_by_default(self) -> None:
        token = begin_trace("memory")
        trace_event("Evolver", "dedup_decision", {"candidate": "c1"}, level=_DEBUG)
        trace_event("Evolver", "dedup", {"add": 1})
        assert _spans(end_trace(token)) == [
            {"component": "Evolver", "stage": "dedup", "data": {"add": 1}}
        ], "info 阈值下 debug 明细不得落树"

    def test_debug_event_kept_and_marked_at_debug(self) -> None:
        token = begin_trace("memory", level="debug")
        trace_event("Evolver", "dedup_decision", {"candidate": "c1"}, level=_DEBUG)
        trace_event("Evolver", "dedup", {"add": 1})
        debug, info = _spans(end_trace(token))
        assert debug["level"] == "debug", "明细节点应标 level"
        assert "level" not in info, "概要节点不加 level 字段"

    def test_debug_span_skipped_children_attach_outer(self) -> None:
        token = begin_trace("memory")
        with trace_span("Engine", "write"):
            with trace_span("Builder", "chunk", level=_DEBUG):
                trace_event("Builder", "built", {"n": 1})
        (root,) = _spans(end_trace(token))
        assert [c["stage"] for c in root["children"]] == ["built"], (
            "低于阈值的 span 不建节点，其内事件挂到外层 span"
        )

    def test_debug_traced_and_annotate_gated(self) -> None:
        class Builder:
            @traced("chunk", level=_DEBUG)
            def chunk(self) -> int:
                trace_annotate({"detail": 1}, level=_DEBUG)
                return 3

        token = begin_trace("memory")
        with trace_span("Engine", "write"):
            assert Builder().chunk() == 3
        (root,) = _spans(end_trace(token))
        assert "children" not in root and "data" not in root, (
            "info 阈值下 debug span/annotate 不生效"
        )

        token = begin_trace("memory", level=_DEBUG)
        assert Builder().chunk() == 3
        (chunk,) = _spans(end_trace(token))
        assert chunk["level"] == "debug" and chunk["data"] == {"detail": 1}, chunk

    def test_config_params_level_is_default_threshold(self) -> None:
        configure_default(TraceProducer.build("memory", {"level": "debug"}, ctx=None))
        token = begin_trace()
        trace_event("C", "s", {}, level=_DEBUG)
        assert len(_spans(end_trace(token))) == 1, "config params.level=debug 应作缺省阈值"

        token = begin_trace(level="info")
        trace_event("C", "s", {}, level=_DEBUG)
        assert end_trace(token) == {}, "请求级 level 应优先于 config"

    def test_config_rejects_unknown_level(self) -> None:
        with pytest.raises(ValidationError, match="trace level"):
            TraceProducer.build("log", {"level": "fatal"}, ctx=None)

    def test_log_line_carries_level(self) -> None:
        with _capture_logger("agent_memory.common.trace.trace_impl.log_trace") as records:
            token = begin_trace("log", level=_DEBUG)
            trace_event("C", "s", {"v": 1}, level=_DEBUG)
            end_trace(token)
        assert any(" debug C.s " in r for r in records), f"日志行应带详细程度: {records}"

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("memory", ("memory", None)),
            ("memory:debug", ("memory", TraceLevel.DEBUG)),
            (":debug", (None, TraceLevel.DEBUG)),
            ("both:INFO", ("both", TraceLevel.INFO)),
        ],
    )
    def test_parse_trace_spec(self, spec: str, expected: tuple) -> None:
        assert parse_trace_spec(spec) == expected

    def test_parse_trace_spec_rejects_unknown_level(self) -> None:
        with pytest.raises(ValidationError, match="trace level"):
            parse_trace_spec("memory:fatal")


@pytest.mark.unit
class TestBounds:
    def test_long_string_truncated(self) -> None:
        token = begin_trace("memory")
        trace_event("C", "s", {"text": "x" * 500})
        value = _spans(end_trace(token))[0]["data"]["text"]
        assert len(value) < 300 and value.endswith("...[truncated]"), "超长字符串应截断"

    def test_nodes_overflow_counted(self) -> None:
        token = begin_trace("memory")
        for i in range(200):
            trace_event("C", "s", {"n": i})
        snapshot = end_trace(token)
        assert len(_spans(snapshot)) == 128, "节点数不得超过上限"
        assert snapshot["dropped"] == 72, "超限节点应计入 dropped 而非静默丢"


@pytest.mark.unit
class TestConfigAndOverride:
    def test_resolve_unknown_target_rejected(self) -> None:
        with pytest.raises(ValidationError, match="未注册"):
            resolve_target("no-such-target")

    def test_configure_default_rejects_non_collector(self) -> None:
        with pytest.raises(ValidationError, match="TraceCollector"):
            configure_default(object())  # type: ignore[arg-type]

    def test_default_used_when_no_override(self) -> None:
        configure_default(resolve_target("memory"))
        token = begin_trace()
        trace_event("C", "s", {"v": 1})
        assert end_trace(token) != {}, "begin_trace() 缺省应使用 config 默认实现"

    def test_override_beats_default(self) -> None:
        configure_default(resolve_target("noop"))
        token = begin_trace("memory")
        trace_event("C", "s", {"v": 1})
        assert end_trace(token) != {}, "请求级覆盖应优先于默认实现"

    def test_shared_instances_are_stateless(self) -> None:
        assert resolve_target("memory") is resolve_target("memory"), "无状态实现应全局共享单例"

    def test_module_exports(self) -> None:
        assert trace_mod.TRACE_OVERRIDE_KEY == "trace"
