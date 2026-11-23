# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""写入链路的降级信号（F10 决策 8）。

四类降级都必须可见：合并抽取失败退回逐条、来源缺失整批共享、等锁超时、引擎不支持
批量写。实现统一走本模块——每条同时输出 WARNING、trace 事件并累加计数器，不往
``BatchWriteOutcome`` 里加字段（公开返回结构不变）。
"""

from __future__ import annotations

import threading
from enum import Enum

from jiuwen_memory.common.log import get_logger, scope_for_log
from jiuwen_memory.common.trace import trace_event
from jiuwen_memory.common.type_def import Scope

logger = get_logger(__name__)


class DegradeEvent(str, Enum):
    """可观测的降级事件。"""

    MERGE_FALLBACK = "merge_fallback"
    SHARED_ATTRIBUTION = "shared_attribution"
    LOCK_TIMEOUT = "lock_timeout"
    ENGINE_DEGRADED = "engine_degraded"
    EXTRACT_UNSUPPORTED = "extract_unsupported"
    BUILD_UNSUPPORTED = "build_unsupported"
    UNKNOWN_NO_REPLAY = "unknown_no_replay"


class WriteSignals:
    """一次装配内的写入降级计数（S09 不变量 4：有状态依赖归装配上下文所有）。

    计数器随实例走：两个 Runtime 各持一份，互不影响；编排器与引擎经装配共享同一实例。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}

    def record(
        self,
        event: DegradeEvent,
        *,
        scope: Scope | None = None,
        batch_size: int = 0,
        reason: str = "",
    ) -> None:
        """记录一次降级：WARNING + trace 事件 + 计数。"""
        with self._lock:
            self._counters[event.value] = self._counters.get(event.value, 0) + 1
        payload = {
            "event": event.value,
            "scope": scope_for_log(scope) if scope is not None else "",
            "batch_size": batch_size,
            "reason": reason,
        }
        logger.warning("write degraded: %s", payload)
        trace_event("WriteOrchestrator", event.value, payload)

    def counters(self) -> dict[str, int]:
        """返回当前计数器快照（拷贝，调用方不得改内部状态）。"""
        with self._lock:
            return dict(self._counters)

    def reset(self) -> None:
        """清空计数器；仅供测试使用。"""
        with self._lock:
            self._counters.clear()
