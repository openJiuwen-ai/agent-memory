# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""日志 trace sink；专用文件按路径共享 handler，按实例引用释放。"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.log import get_logger, install_privacy_filter, redact_for_log
from jiuwen_memory.common.trace.trace import TraceCollector, TraceLevel, TraceProducer, level_param

_LOG = get_logger(__name__)
_FILES: dict[str, tuple[logging.Logger, logging.FileHandler, int]] = {}
_LOCK = RLock()
# 技术 ID / 数值可留日志；正文、查询和用户过滤值只在调用方的内存快照中保留。
_PRIVATE_FIELDS = frozenset({
    "raw", "rewritten", "tokens", "keywords", "filters", "content", "text", "items",
    "error", "entities", "intent", "raw_response", "query", "query_context", "extracted",
})


def _log_data(data: Mapping[str, Any]) -> dict[str, Any]:
    def protect(value: Any, key: str = "") -> Any:
        if key in _PRIVATE_FIELDS:
            return redact_for_log(value)
        if isinstance(value, Mapping):
            return {k: protect(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [protect(v) for v in value]
        return value

    return protect(data)


class LogTraceCollector(TraceCollector):
    def __init__(self, level: TraceLevel = TraceLevel.INFO, *, log_file: str | None = None) -> None:
        super().__init__(level)
        self._file: str | None = None
        self._logger = _LOG
        self._closed = False
        if log_file is None:
            return
        if not isinstance(log_file, str) or not log_file.strip():
            raise ValidationError("trace log_file 必须是非空路径或 null")
        path = str(Path(log_file).expanduser().resolve())
        with _LOCK:
            entry = _FILES.get(path)
            if entry is None:
                try:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    handler = logging.FileHandler(path, encoding="utf-8")
                except OSError as exc:
                    raise ValidationError("trace log_file 无法打开") from exc
                name = "agent_memory.trace.file." + hashlib.sha256(path.encode()).hexdigest()
                logger = logging.getLogger(name)
                logger.setLevel(logging.INFO)
                logger.propagate = False
                handler.setFormatter(logging.Formatter(
                    "[%(asctime)s] %(name)s %(levelname)s %(message)s"
                ))
                install_privacy_filter(handler)
                logger.addHandler(handler)
                entry = (logger, handler, 0)
            self._logger, handler, references = entry
            _FILES[path] = (self._logger, handler, references + 1)
            self._file = path

    def record(self, trace_id: str, path: str, data: Mapping[str, Any], level: TraceLevel) -> None:
        if not self._closed:
            self._logger.info("trace[%s] %s %s %s", trace_id, level.value, path, _log_data(data))

    def close(self) -> None:
        with _LOCK:
            if self._closed:
                return
            self._closed = True
            if self._file is None:
                return
            logger, handler, references = _FILES[self._file]
            if references > 1:
                _FILES[self._file] = (logger, handler, references - 1)
            else:
                logger.removeHandler(handler)
                handler.flush()
                handler.close()
                del _FILES[self._file]


@TraceProducer.register("log")
def _build(config) -> TraceCollector:
    return LogTraceCollector(level_param(config), log_file=config.params.get("log_file"))
