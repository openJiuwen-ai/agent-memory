# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""memory_log trace：log 的 sink 行为 + 保留请求级调用树（组合，不另写日志逻辑）。"""

from __future__ import annotations

from jiuwen_memory.common.trace.trace import TraceCollector, TraceProducer, level_param
from jiuwen_memory.common.trace.trace_impl.log_trace import LogTraceCollector


class MemoryLogTraceCollector(LogTraceCollector):
    buffers = True


@TraceProducer.register("memory_log")
def _build(config) -> TraceCollector:
    return MemoryLogTraceCollector(level_param(config), log_file=config.params.get("log_file"))
