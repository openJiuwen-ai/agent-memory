# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""noop trace：默认实现，不开作用域（埋点全部短路）、不落任何 sink。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwen_memory.common.trace.trace import (
    TraceCollector,
    TraceLevel,
    TraceProducer,
    level_param,
)


class NoopTraceCollector(TraceCollector):
    enabled = False

    def record(self, trace_id: str, path: str, data: Mapping[str, Any], level: TraceLevel) -> None:
        return None


@TraceProducer.register("noop")
def _build(config) -> TraceCollector:
    return NoopTraceCollector(level=level_param(config))
