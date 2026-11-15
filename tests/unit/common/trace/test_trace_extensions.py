# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Trace 插件、三级过滤、共享预算、文件与后台生命周期回归。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.trace import (
    TraceCollector,
    TraceLevel,
    TraceLimits,
    TraceProducer,
    begin_trace,
    capture_trace,
    configure_default,
    end_trace,
    level_param,
    noop_collector,
    resolve_target,
    trace_annotate,
    trace_event,
    trace_span,
)
from jiuwen_memory.common.trace.runtime import TraceRuntime, background_trace, bind_trace
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.retrieval.trace_projection import trajectory_steps

register_plugins()

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def reset_trace():
    yield
    configure_default(resolve_target("noop"))


@pytest.mark.parametrize(
    "threshold,expected",
    [
        ("simple", ["simple"]),
        ("info", ["simple", "info"]),
        ("debug", ["simple", "info", "debug"]),
    ],
)
def test_three_levels_filter_events_spans_annotations_before_data_copy(threshold, expected):
    evaluated = []
    token = begin_trace("memory", level=threshold)
    try:
        with trace_span("Test", "root", level=TraceLevel.SIMPLE):
            for level in TraceLevel:
                trace_event(
                    "Test",
                    level.value,
                    lambda value=level: evaluated.append(value.value) or {"v": value.value},
                    level=level,
                )
                with trace_span("Test", "span_" + level.value, level=level):
                    trace_annotate({level.value: True}, level=level)
        snapshot = end_trace(token)
    except BaseException:
        end_trace(token)
        raise
    assert evaluated == expected, "被过滤的数据不得求值"
    children = snapshot["spans"][0]["children"]
    assert [n["stage"] for n in children if not n["stage"].startswith("span_")] == expected
    assert all(n.get("level", "info") in expected for n in children)


def test_factory_plugin_named_dependency_and_replacement():
    from jiuwen_memory.config.context import AssemblyContext

    class CustomTrace(TraceCollector):
        buffers = True

        def __init__(self, config):
            super().__init__(level_param(config))
            self.label = config.params["label"]

        def record(self, trace_id, path, data, level):
            return None

    with TraceProducer.registration_scope():
        TraceProducer.register("custom")(CustomTrace)
        ctx = AssemblyContext.from_dict(
            {
                "trace": {
                    "external": {
                        "target": "custom",
                        "params": {"level": "simple", "label": "customer"},
                    }
                }
            }
        )
        instance = TraceProducer.build_named("external", ctx)
        assert instance is TraceProducer.build_named("external", ctx)
        assert instance.label == "customer"
        token = begin_trace(collector=instance)
        trace_event("Test", "summary", {"n": 2}, level=TraceLevel.SIMPLE)
        trace_event("Test", "info", {})
        assert len(end_trace(token)["spans"]) == 1
        with pytest.raises(ValidationError, match="互斥"):
            begin_trace("memory", collector=instance)
        # 新 builder 覆盖后不能命中旧 target 缓存。
        TraceProducer.register("replaceable")(lambda cfg: resolve_target("memory"))
        first = resolve_target("replaceable")
        TraceProducer.register("replaceable")(lambda cfg: resolve_target("noop"))
        assert resolve_target("replaceable") is not first


@pytest.mark.parametrize("target", ["noop", "memory", "log", "memory_log"])
def test_trajectory_capture_keeps_precision_and_does_not_enable_response(target):
    token = begin_trace(target, level="simple")
    try:
        with capture_trace() as capture:
            trace_event(
                "Custom",
                "stage",
                {},
                projection={
                    "stage": "custom",
                    "channel": None,
                    "candidate_count": 2,
                    "cost_ms": 1.123456789,
                    "detail": {"stage": "user", "candidates": "collision"},
                },
            )
            steps = trajectory_steps(capture)
        snapshot = end_trace(token)
    except BaseException:
        end_trace(token)
        raise
    assert steps[0].cost_ms == 1.123456789
    assert steps[0].detail == {"stage": "user", "candidates": "collision"}
    assert snapshot == {}, "轨迹要求不得提升通用 trace 的详细程度或响应能力"


def test_threaded_capture_shares_budget_and_ends_without_late_writes():
    token = begin_trace("memory", limits=TraceLimits(max_nodes=3))
    with capture_trace() as capture:

        def record(n):
            trace_event(
                "Test",
                "stage",
                {"n": n},
                projection={
                    "stage": "stage",
                    "channel": None,
                    "candidate_count": n,
                    "cost_ms": 0.123456789,
                    "detail": {},
                },
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(copy_context().run, record, n) for n in range(10)]
            for future in futures:
                future.result()
        assert len(capture.records) == 3
        assert capture.dropped == 7
    snapshot = end_trace(token)
    assert snapshot["dropped"] == 7
    assert len(snapshot["spans"]) == 3


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "2"])
def test_invalid_limits(value):
    with pytest.raises(ValidationError, match="正整数"):
        TraceLimits(max_nodes=value)


def test_all_limits_can_be_disabled():
    token = begin_trace("memory", limits=TraceLimits(None, None, None))
    for _ in range(200):
        trace_event("Test", "stage", {"text": "x" * 1000, "deep": {"a": {"b": {"c": [1]}}}})
    snapshot = end_trace(token)
    assert len(snapshot["spans"]) == 200
    assert snapshot["spans"][0]["data"]["text"] == "x" * 1000
    assert snapshot["spans"][0]["data"]["deep"]["a"]["b"]["c"] == [1]


def test_file_sink_shared_released_and_private_fields_protected(tmp_path):
    from jiuwen_memory.common.trace.trace_impl.log_trace import _FILES

    path = tmp_path / "trace.log"
    first = TraceProducer.build("log", {"log_file": str(path)}, None)
    second = TraceProducer.build("memory_log", {"log_file": str(path)}, None)
    assert _FILES[str(path)][2] == 2
    token = begin_trace(collector=first, level="debug")
    trace_event("Test", "query", {"query": "private coffee", "returned": 2}, level=TraceLevel.DEBUG)
    end_trace(token)
    first.close()
    token = begin_trace(collector=second)
    trace_event("Test", "result", {"n": 3})
    assert end_trace(token)["spans"][0]["data"] == {"n": 3}
    second.close()
    second.close()
    assert str(path) not in _FILES
    text = path.read_text()
    assert "private coffee" not in text
    assert "returned" in text and "Test.result" in text
    assert text.count("Test.query") == 1


def test_noop_collector_does_not_require_plugin_registration():
    # 插件尚未 bootstrap 或被应用替换时，未装配兜底也必须保持禁用。
    def unavailable(config):
        pytest.fail("noop_collector 不得调用插件 builder")

    with TraceProducer.registration_scope():
        TraceProducer.register("noop")(unavailable)
        collector = noop_collector()
        token = begin_trace(collector=collector)
        try:
            trace_event("Worker", "unused", lambda: pytest.fail("禁用时不得求值数据"))
        finally:
            snapshot = end_trace(token)
    assert not collector.enabled, "未装配兜底必须禁用采集"
    assert snapshot == {}, "禁用作用域不得产生快照"


@pytest.mark.parametrize("cancelled", [False, True])
def test_unbound_background_suppresses_parent_and_process_default(cancelled):
    records = []

    class Recording(TraceCollector):
        buffers = True

        def record(self, trace_id, path, data, level):
            records.append((trace_id, path))

    class Owner:
        pass

    configure_default(Recording())
    outer = begin_trace(trace_id="online")
    try:
        with trace_span("Outer", "run"):
            def run_job():
                with background_trace(Owner(), Scope(org="demo"), "job-1", "evolve"):
                    trace_event("Worker", "count", {"n": 2})
                    if cancelled:
                        raise asyncio.CancelledError

            if cancelled:
                with pytest.raises(asyncio.CancelledError):
                    run_job()
            else:
                run_job()
            trace_event("Outer", "done", {})
    finally:
        snapshot = end_trace(outer)
    assert [n["stage"] for n in snapshot["spans"][0]["children"]] == ["done"], (
        "后台事件不得串入在线请求，退出后台作用域后应恢复外层 span"
    )
    assert records == [("online", "Outer.run/Outer.done"), ("online", "Outer.run")], (
        "未装配后台执行器不得使用进程默认 sink"
    )


def test_background_scope_uses_selected_plugin_and_restores_parent():
    class Recording(TraceCollector):
        def record(self, trace_id, path, data, level):
            records.append((trace_id, path, data))

    records = []
    collector = Recording()
    runtime = TraceRuntime(
        resolve_target("noop"), named={"jobs": collector}, job_types={"custom": "jobs"}
    )

    class Owner:
        pass

    owner = Owner()
    bind_trace(owner, runtime)
    outer = begin_trace("memory", trace_id="online")
    try:
        with pytest.raises(asyncio.CancelledError):
            with background_trace(owner, Scope(org="a|b", user="u="), "job|1", "custom"):
                trace_event("Worker", "count", {"n": 2})
                raise asyncio.CancelledError
        trace_event("Outer", "done", {})
    finally:
        snapshot = end_trace(outer)
    assert [n["component"] for n in snapshot["spans"]] == ["Outer"]
    assert all("org=a%7Cb" in r[0] and "job_id=job%7C1" in r[0] for r in records)
    assert records[-1][2]["error"] == "CancelledError"


@pytest.mark.parametrize(
    "policy",
    [
        None,
        [],
        {"types": []},
        {"types": {"evolve": "missing"}},
        {"default": None},
        {"unknown": "memory"},
    ],
)
def test_invalid_job_policy_rejected_at_assembly(policy):
    from jiuwen_memory.common.trace.runtime import build_trace_runtime
    from jiuwen_memory.config.context import AssemblyContext, ComponentConfig

    ctx = AssemblyContext.from_dict(
        {
            "globals": {"trace_jobs": policy},
            "trace": {"default": "noop"},
        }
    )
    with pytest.raises(ValidationError, match="trace_jobs"):
        build_trace_runtime(ComponentConfig(params={"trace": "default"}, ctx=ctx))


def test_noop_preserves_limits_for_temporary_capture():
    token = begin_trace("noop", limits=TraceLimits(max_nodes=1, max_value_chars=2))
    try:
        with capture_trace() as capture:
            for _ in range(3):
                trace_event(
                    "Test",
                    "stage",
                    {},
                    projection={
                        "stage": "stage",
                        "channel": "keyword",
                        "candidate_count": 2,
                        "cost_ms": 0.1,
                        "detail": {"query": "abcdef"},
                    },
                )
            steps = trajectory_steps(capture)
            assert steps[0].channel.value == "keyword"
            assert steps[0].detail["query"] == "ab...[truncated]"
            assert steps[-1].detail["dropped"] == "2"
    finally:
        assert end_trace(token) == {}


def test_runtime_releases_file_sink_after_close_and_on_assembly_failure(tmp_path):
    from jiuwen_memory.api.memory_api_impl import assemble_runtime
    from jiuwen_memory.common.trace.trace_impl.log_trace import _FILES
    from jiuwen_memory.config import Config

    path = str(tmp_path / "owned.log")
    runtime = assemble_runtime(
        config=Config.from_dict(
            {
                "trace": {"default": {"target": "log", "params": {"log_file": path}}},
            }
        )
    )
    assert path in _FILES
    runtime.close()
    assert path not in _FILES
    with pytest.raises(ValidationError):
        assemble_runtime(
            config=Config.from_dict(
                {
                    "globals": {"trace_jobs": {"default": "absent"}},
                    "trace": {"default": {"target": "log", "params": {"log_file": path}}},
                }
            )
        )
    assert path not in _FILES, "失败装配也必须关闭新建文件 sink"


def test_debug_values_are_json_serializable_and_do_not_keep_mutable_aliases():
    import json
    from datetime import UTC, datetime

    values = {
        "date": datetime(2026, 11, 12, tzinfo=UTC),
        "level": TraceLevel.DEBUG,
        "list": [1],
        "opaque": object(),
    }
    token = begin_trace("memory", level="debug")
    trace_event("Test", "details", values, level=TraceLevel.DEBUG)
    values["list"].append(2)
    snapshot = end_trace(token)
    assert snapshot["spans"][0]["data"]["list"] == [1]
    assert "2026-11-12" in json.dumps(snapshot)


def test_closed_capture_rejects_context_copied_inside_it():
    outer = begin_trace("memory")
    try:
        with capture_trace() as capture:
            captured = copy_context()
        captured.run(
            trace_event,
            "Test",
            "late",
            {},
            projection={
                "stage": "late",
                "channel": None,
                "candidate_count": 1,
                "cost_ms": 0.0,
                "detail": {},
            },
        )
        assert capture.records == [], "局部捕获结束后复制 context 也不得追加投影"
    finally:
        end_trace(outer)


def test_scheduler_executes_using_bound_job_trace_policy_and_separate_id():
    from jiuwen_memory.control.jobs import Job
    from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler
    from jiuwen_memory.control.types import Channel, JobInfo, JobStatus

    records = []

    class Sink(TraceCollector):
        def record(self, trace_id, path, data, level):
            records.append((trace_id, path, dict(data)))

    class CustomJob(Job):
        @property
        def trace_type(self):
            return "custom"

        async def run(self):
            trace_event("Custom", "result", {"n": 1}, level=TraceLevel.SIMPLE)
            return JobInfo(
                id="ignored", scope=self.scope, status=JobStatus.SUCCEEDED, channel=Channel.HOT
            )

    scheduler = InProcessScheduler()
    bind_trace(
        scheduler,
        TraceRuntime(
            resolve_target("noop"), named={"custom": Sink()}, job_types={"custom": "custom"}
        ),
    )
    outer = begin_trace("memory", trace_id="request")
    try:
        job_id = asyncio.run(scheduler.submit(CustomJob(scope=Scope(org="o")), Channel.HOT))
        assert scheduler.status(job_id).status == JobStatus.SUCCEEDED
    finally:
        assert end_trace(outer) == {}, "后台不向触发请求写事件"
    assert any(path.endswith("Custom.result") for _, path, _ in records)
    assert all("job_id=" + job_id in trace_id for trace_id, _, _ in records)
