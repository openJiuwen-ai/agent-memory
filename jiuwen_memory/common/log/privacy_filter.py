# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一的运行日志敏感值标记与脱敏 Filter。

业务代码只负责通过 ``redact_for_log`` / ``metadata_for_log`` /
``scope_for_log`` 标明参数的语义；真正的替换在 :class:`SensitiveDataFilter`
中、Formatter 生成最终文本之前统一完成。业务对象和持久化数据不会被修改。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from types import TracebackType
from typing import Any

LOG_MASK = "*"

_SCOPE_FIELD_NAMES = ("org", "space", "user", "agent", "session")

# 只允许明确属于 MemoryUnit 的技术 ID 字段。不能使用 endswith("_id")，
# 因为 user_id/session_id/org_id 等身份字段同样满足该后缀，但必须脱敏。
_MEMORY_UNIT_ID_FIELD_NAMES = frozenset(
    {
        "candidate_id",
        "candidate_ids",
        "child_clm_source_ids",
        "created_id",
        "created_ids",
        "dedup_merged_from",
        "dedup_superseded",
        "existing_unit_id",
        "existing_unit_ids",
        "forgotten_id",
        "forgotten_ids",
        "memory_unit_id",
        "memory_unit_ids",
        "new_id",
        "new_ids",
        "old_id",
        "old_ids",
        "provenance",
        "source_id",
        "source_ids",
        "source_unit_id",
        "source_unit_ids",
        "superseded_id",
        "superseded_ids",
        "target_id",
        "target_ids",
        "target_unit_id",
        "target_unit_ids",
        "unit_id",
        "unit_ids",
        "updated_id",
        "updated_ids",
    }
)


def _field_leaf(field_name: str) -> str:
    """兼容 ``system_metadata.unit_id`` 一类带命名空间的字段名。"""
    return field_name.strip().lower().rsplit(".", maxsplit=1)[-1]


def _visible_memory_unit_id(
    value: Any,
    visible_memory_unit_ids: frozenset[str | int],
) -> Any:
    """只复制调用方明确确认的 MemoryUnit ID。"""
    if isinstance(value, bool):
        return LOG_MASK
    if isinstance(value, (str, int)):
        return value if value in visible_memory_unit_ids else LOG_MASK
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _visible_memory_unit_id(item, visible_memory_unit_ids) for item in value
        ]
    return LOG_MASK


def _safe_key(key: Any) -> str:
    if isinstance(key, str):
        return key
    return f"<unsupported_key_type:{type(key).__name__}>"


def _sanitize_metadata(
    value: Any,
    *,
    field_name: str | None,
    visible_memory_unit_ids: frozenset[str | int],
) -> Any:
    if field_name is not None and _field_leaf(field_name) in (
        _MEMORY_UNIT_ID_FIELD_NAMES
    ):
        return _visible_memory_unit_id(value, visible_memory_unit_ids)

    if isinstance(value, Mapping):
        try:
            result = {}
            for key, item in value.items():
                key_text = _safe_key(key)
                result[key_text] = _sanitize_metadata(
                    item,
                    field_name=key_text,
                    visible_memory_unit_ids=visible_memory_unit_ids,
                )
            return result
        except Exception:
            # 日志脱敏不能反向影响业务流程；自定义 Mapping 读取失败时整体隐藏。
            return LOG_MASK
    if isinstance(value, (list, tuple, set, frozenset)):
        try:
            return [
                _sanitize_metadata(
                    item,
                    field_name=field_name,
                    visible_memory_unit_ids=visible_memory_unit_ids,
                )
                for item in value
            ]
        except Exception:
            return LOG_MASK
    return LOG_MASK


class _ProtectedLogValue:
    """只由本模块创建的日志参数标记；其 repr/str 本身也不会泄露原值。"""

    __slots__ = ("_kind", "_value", "_visible_memory_unit_ids")

    def __init__(
        self,
        kind: str,
        value: Any = None,
        visible_memory_unit_ids: Iterable[str | int] = (),
    ) -> None:
        self._kind = kind
        self._value = value
        self._visible_memory_unit_ids = frozenset(visible_memory_unit_ids)

    def resolve(self) -> Any:
        if self._kind == "metadata":
            return _sanitize_metadata(
                self._value,
                field_name=None,
                visible_memory_unit_ids=self._visible_memory_unit_ids,
            )
        if self._kind == "scope":
            return {field_name: LOG_MASK for field_name in _SCOPE_FIELD_NAMES}
        return LOG_MASK

    def __str__(self) -> str:
        return str(self.resolve())

    def __repr__(self) -> str:
        return repr(self.resolve())


_REDACTED_VALUE = _ProtectedLogValue("redact")
_REDACTED_SCOPE = _ProtectedLogValue("scope")


def redact_for_log(value: Any) -> _ProtectedLogValue:
    """标记一项完全不可出现在日志中的值，最终固定显示 ``*``。"""
    del value
    return _REDACTED_VALUE


def metadata_for_log(
    value: Any,
    *,
    visible_memory_unit_ids: Iterable[str | int] = (),
) -> _ProtectedLogValue:
    """标记 metadata 类参数。

    Filter 会保留映射字段名、列表层级和空容器；所有普通叶子值显示为 ``*``。
    MemoryUnit ID 只有同时满足字段白名单，并由调用点通过
    ``visible_memory_unit_ids`` 明确确认时才保留。
    """
    return _ProtectedLogValue("metadata", value, visible_memory_unit_ids)


def scope_for_log(scope: Any) -> _ProtectedLogValue:
    """标记 Scope；不读取真实值，仅显示五个固定维度和 ``*``。"""
    del scope
    return _REDACTED_SCOPE


def _resolve_markers(value: Any) -> Any:
    if isinstance(value, _ProtectedLogValue):
        return value.resolve()
    # 异常消息可能夹带请求正文、远端响应或密钥。异常对象统一只保留类型名；
    # 正常的错误定位上下文（组件、操作、技术 ID）仍由固定日志模板提供。
    if isinstance(value, BaseException):
        return type(value).__name__
    if isinstance(value, tuple):
        return tuple(_resolve_markers(item) for item in value)
    if isinstance(value, list):
        return [_resolve_markers(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_markers(item) for key, item in value.items()}
    return value


def _redacted_exception_text(
    exception_type: type[BaseException] | None,
    exception_traceback: TracebackType | None,
) -> str:
    """保留文件、行号和调用关系，但不格式化异常值或源码行。"""
    type_name = getattr(exception_type, "__name__", "Exception")
    stack_lines: list[str] = []
    current = exception_traceback
    while current is not None:
        code = current.tb_frame.f_code
        stack_lines.append(
            f'  File "{code.co_filename}", line {current.tb_lineno}, in {code.co_name}\n'
        )
        current = current.tb_next
    if not stack_lines:
        return f"{type_name}: {LOG_MASK}"
    stack = "".join(stack_lines)
    return f"Traceback (most recent call last):\n{stack}{type_name}: {LOG_MASK}"


class SensitiveDataFilter(logging.Filter):
    """在日志 Formatter 运行前统一解析敏感参数标记。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _resolve_markers(record.msg)
        record.args = _resolve_markers(record.args)
        if record.exc_info is not None:
            exception_type, _, exception_traceback = record.exc_info
            record.exc_info = (
                exception_type,
                Exception(LOG_MASK),
                exception_traceback,
            )
            record.exc_text = _redacted_exception_text(
                exception_type,
                exception_traceback,
            )
        return True


_PRIVACY_FILTER = SensitiveDataFilter()


def install_privacy_filter(target: logging.Logger | logging.Handler) -> None:
    """把全局唯一 Filter 安装到 logger 或 handler，重复调用保持幂等。"""
    if _PRIVACY_FILTER not in target.filters:
        target.addFilter(_PRIVACY_FILTER)
