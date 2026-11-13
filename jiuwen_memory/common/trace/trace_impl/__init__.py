# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TraceCollector 实现注册触发（import 各实现模块完成自注册）。"""

from jiuwen_memory.common.trace.trace_impl import (  # noqa: F401
    both_trace,
    log_trace,
    memory_trace,
    noop_trace,
)
