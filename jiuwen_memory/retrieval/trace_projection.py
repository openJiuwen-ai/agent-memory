# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""检索阶段从统一 Trace 记录投影为轨迹；common 不依赖检索 DTO。"""

from collections.abc import Callable
from contextlib import nullcontext
from functools import wraps
from typing import Any

from jiuwen_memory.common.trace import TraceLevel, capture_trace, trace_enabled, trace_event
from jiuwen_memory.common.trace.trace import TraceCapture
from jiuwen_memory.retrieval.types import RecallChannel, TrajectoryStep


def trajectory_steps(capture: TraceCapture) -> list[TrajectoryStep]:
    steps = [
        TrajectoryStep(
            stage=record["stage"],
            channel=RecallChannel(record["channel"]) if record["channel"] else None,
            candidate_count=record["candidate_count"],
            cost_ms=record["cost_ms"],
            detail=record["detail"],
        )
        for record in capture.records
    ]
    if capture.dropped:
        steps.append(
            TrajectoryStep(
                stage="trace_truncated",
                detail={"reason": "max_nodes", "dropped": str(capture.dropped)},
            )
        )
    return steps


def record_step(
    component: str,
    step: TrajectoryStep,
    *,
    enabled: bool,
    level: TraceLevel = TraceLevel.INFO,
) -> None:
    trace_event(
        component,
        step.stage,
        lambda: {
            "candidates": step.candidate_count,
            "cost_ms": round(step.cost_ms, 3),
            **({"channel": step.channel.value} if step.channel else {}),
            **(step.detail if trace_enabled(TraceLevel.INFO) else {}),
        },
        level=level,
        projection={
            "stage": step.stage,
            "channel": step.channel.value if step.channel else None,
            "candidate_count": step.candidate_count,
            "cost_ms": step.cost_ms,
            "detail": step.detail,
        }
        if enabled
        else None,
    )


def append_step(
    trajectory: list[TrajectoryStep],
    component: str,
    step: TrajectoryStep,
    *,
    enabled: bool,
) -> None:
    """延迟展开在原检索返回后运行时也使用受限记录。"""
    if enabled:
        with capture_trace() as capture:
            record_step(component, step, enabled=True)
            trajectory.extend(trajectory_steps(capture))
    else:
        record_step(component, step, enabled=False)


def capture_retrieval(fn: Callable) -> Callable:
    """完整 search 边界统一预算，保留子检索定义的兼容轨迹顺序。"""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        enabled = kwargs.get("with_trajectory", False)
        with capture_trace() if enabled else nullcontext() as capture:
            result = fn(*args, **kwargs)
            if capture is not None and capture.dropped:
                result.trajectory = [s for s in result.trajectory if s.stage != "trace_truncated"]
                result.trajectory.append(
                    TrajectoryStep(
                        stage="trace_truncated",
                        detail={"reason": "max_nodes", "dropped": str(capture.dropped)},
                    )
                )
            return result

    return wrapper
