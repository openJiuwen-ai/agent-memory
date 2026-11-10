# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""memory trace：收集请求级调用树，供请求返回前填充进 response。

调用树只返回给本次请求的发起者本人，不做内容审查；体积护栏
（节点数上限/字符串截断）由框架在写入前统一执行。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwen_memory.common.trace.trace import (
    TraceCollector,
    TraceLevel,
    TraceProducer,
    level_param,
)


class MemoryTraceCollector(TraceCollector):
    buffers = True

    def record(self, trace_id: str, path: str, data: Mapping[str, Any], level: TraceLevel) -> None:
        return None  # 调用树由框架维护


@TraceProducer.register("memory")
def _build(config) -> TraceCollector:
    return MemoryTraceCollector(level=level_param(config))
