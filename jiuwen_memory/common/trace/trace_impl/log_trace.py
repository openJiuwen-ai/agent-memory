# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""log trace：记录时写入统一日志，不保留请求级调用树。

经 ``get_logger`` 进入 ``agent_memory.`` 命名空间；每行带 trace_id、详细程度与根到本节点的
path，同一请求的多行可按 trace_id 串起。框架只做体积截断，不做内容审查，
埋点纪律约束 data 不得包含正文/凭证。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.trace.trace import (
    TraceCollector,
    TraceLevel,
    TraceProducer,
    level_param,
)

_LOG = get_logger(__name__)


class LogTraceCollector(TraceCollector):
    def record(self, trace_id: str, path: str, data: Mapping[str, Any], level: TraceLevel) -> None:
        _LOG.info("trace[%s] %s %s %s", trace_id, level.value, path, dict(data))


@TraceProducer.register("log")
def _build(config) -> TraceCollector:
    return LogTraceCollector(level=level_param(config))
