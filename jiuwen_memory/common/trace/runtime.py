# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Trace 装配策略：具名插件、请求绑定与后台任务独立作用域。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from weakref import WeakKeyDictionary

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.trace.trace import (
    TraceCollector,
    TraceLevel,
    TraceLimits,
    TraceProducer,
    TraceToken,
    begin_trace,
    end_trace,
    noop_collector,
    trace_annotate,
    trace_span,
)


@dataclass
class TraceRuntime:
    default: TraceCollector
    limits: TraceLimits = field(default_factory=TraceLimits)
    named: dict[str, TraceCollector] = field(default_factory=dict)
    job_default: str | None = None
    job_types: dict[str, str] = field(default_factory=dict)

    def begin(
        self,
        target: str | None = None,
        *,
        level: TraceLevel | None = None,
    ) -> TraceToken:
        if target is None:
            return begin_trace(collector=self.default, level=level, limits=self.limits)
        # 覆盖可引用完整具名配置；裸注册名仍按 Factory 匿名构建。
        if target in self.named:
            return begin_trace(collector=self.named[target], level=level, limits=self.limits)
        return begin_trace(target, level=level, limits=self.limits)

    @contextmanager
    def job(
        self,
        scope: Any,
        job_id: str,
        job_type: str,
        parent_job_id: str = "",
    ) -> Iterator[None]:
        name = self.job_types.get(job_type, self.job_default)
        collector = self.named[name] if name is not None else self.default
        fields = ("org", "space", "user", "agent", "session")
        coordinates = "|".join(
            f"{key}={quote(getattr(scope, key, '') or '', safe='-._~')}" for key in fields
        )
        trace_id = f"job|{coordinates}|job_id={quote(job_id, safe='-._~')}"
        token = begin_trace(trace_id=trace_id, collector=collector, limits=self.limits)
        try:
            with trace_span(
                "Job",
                "run",
                {
                    "task_type": job_type,
                    "job_id": job_id,
                    "execution_id": str(uuid.uuid4()),
                    "parent_job_id": parent_job_id,
                },
                level=TraceLevel.SIMPLE,
            ):
                try:
                    yield
                except BaseException as exc:
                    trace_annotate(
                        {
                            "status": "cancelled"
                            if type(exc).__name__
                            in {
                                "CancelledError",
                                "JobCancelledError",
                                "IngestCancelled",
                            }
                            else "failed"
                        },
                        level=TraceLevel.SIMPLE,
                    )
                    raise
                else:
                    trace_annotate({"status": "completed"}, level=TraceLevel.SIMPLE)
        finally:
            end_trace(token)


def build_trace_runtime(config: Any) -> TraceRuntime:
    """只读启动配置；同名实例经标准 Factory 缓存共享。"""
    ctx = config.ctx
    limits = TraceLimits(
        **{
            name: ctx.globals.get("trace_" + name, default)
            for name, default in (("max_nodes", 128), ("max_value_chars", 256), ("max_depth", 4))
        }
    )
    named = {name: TraceProducer.build_named(name, ctx) for name in ctx.namespaces.get("trace", {})}
    if any(not isinstance(instance, TraceCollector) for instance in named.values()):
        raise ValidationError("trace 插件必须返回 TraceCollector")
    default = TraceProducer.dep(config, default="noop")
    policy = ctx.globals.get("trace_jobs", {})
    if not isinstance(policy, Mapping) or policy.keys() - {"default", "types"}:
        raise ValidationError("trace_jobs 必须包含 default / types 映射")
    types = policy.get("types", {})
    if not isinstance(types, Mapping) or any(not isinstance(key, str) or not key for key in types):
        raise ValidationError("trace_jobs.types 必须是任务类型到实例名的映射")
    references = list(types.values())
    if "default" in policy:
        references.append(policy["default"])
    for name in references:
        if not isinstance(name, str) or name not in named:
            raise ValidationError(f"trace_jobs 未知 trace 实例: {name!r}")
    return TraceRuntime(default, limits, named, policy.get("default"), dict(types))


_BOUND: WeakKeyDictionary = WeakKeyDictionary()


def bind_trace(owner: object, runtime: TraceRuntime) -> None:
    """装配根绑定 API / 执行器，不向业务参数传 collector。"""
    if owner is not None:
        _BOUND[owner] = runtime


def _bound_runtime(owner: object) -> TraceRuntime | None:
    try:
        return _BOUND.get(owner)
    except TypeError:
        # 测试/第三方 Access 可以使用不支持 weakref 或 hash 的 API 替身。
        return None


def begin_api_trace(
    api: object,
    target: str | None = None,
    *,
    level: TraceLevel | None = None,
) -> TraceToken:
    runtime = _bound_runtime(api)
    return runtime.begin(target, level=level) if runtime else begin_trace(target, level=level)


@contextmanager
def background_trace(
    owner: object,
    scope: Any,
    job_id: str,
    job_type: str,
    parent_job_id: str = "",
) -> Iterator[None]:
    runtime = _bound_runtime(owner)
    if runtime is None:
        # 未装配的独立执行器仍必须屏蔽触发请求，不继承其默认 collector。
        runtime = TraceRuntime(noop_collector())
    with runtime.job(scope, job_id, job_type, parent_job_id):
        yield
