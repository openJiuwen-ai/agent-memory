# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""both trace：log 的 sink 行为 + 保留请求级调用树（组合，不另写日志逻辑）。"""

from __future__ import annotations

from jiuwen_memory.common.trace.trace import TraceCollector, TraceProducer, level_param
from jiuwen_memory.common.trace.trace_impl.log_trace import LogTraceCollector


class BothTraceCollector(LogTraceCollector):
    buffers = True


@TraceProducer.register("both")
def _build(config) -> TraceCollector:
    return BothTraceCollector(level=level_param(config))
