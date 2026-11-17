# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一日志接口与进程级 SDK 日志接管。"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import UserList, deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.log.privacy_filter import (
    SensitiveDataFilter,
    install_privacy_filter,
    write_diagnostic,
)

_LOG_PREFIX = "agent_memory"
_SDK_LOG_ROOTS = (_LOG_PREFIX, "jiuwen_memory")
_DEFAULT_FORMAT = "[%(asctime)s] %(name)s %(levelname)s %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"
_CONFIG_UNSET = object()
_STATE_LOCK = threading.RLock()
_DISPATCH_LOCAL = threading.local()


@dataclass(frozen=True)
class _LogFilterRules:
    include: str | None = None
    exclude: str | None = None


@dataclass(frozen=True)
class _LogFilters:
    business: _LogFilterRules = _LogFilterRules()
    builtin: _LogFilterRules = _LogFilterRules()


@dataclass(frozen=True)
class _CompiledLogFilter:
    include: re.Pattern[str] | None = None
    exclude: re.Pattern[str] | None = None


@dataclass(frozen=True)
class _LogSettings:
    level: int
    log_format: str
    datefmt: str
    log_file: str
    output: str
    filters: _LogFilters


@dataclass(frozen=True)
class _LoggingState:
    settings: _LogSettings
    business_handler: logging.Handler | None
    router: _DispatchHandler
    owned_handlers: tuple[logging.Handler, ...]


_LOGGING_STATE: _LoggingState | None = None


def get_logger(name: str) -> logging.Logger:
    """获取统一命名的 logger：``agent_memory.<name>``。

    ``name`` 通常传 ``__name__``（模块全限定名），产出层级清晰的 logger 名。
    ``jiuwen_memory.*`` 直取的标准 logger 也会在 SDK 装配时被接管。
    """
    clean = name.removeprefix("jiuwen_memory.").removeprefix("src.")
    full_name = f"{_LOG_PREFIX}.{clean}" if clean else _LOG_PREFIX
    logger = logging.getLogger(full_name)
    install_privacy_filter(logger)
    return logger


def _config_value(config: Any, key: str) -> Any:
    """读取调用方显式配置值；不把 ComponentConfig 的 globals 回退默认混为显式值。"""
    if config is None:
        return _CONFIG_UNSET

    params = getattr(config, "params", None)
    if isinstance(params, Mapping) and key in params:
        return params[key]
    context = getattr(config, "ctx", None)
    globals_ = getattr(context, "globals", None)
    if isinstance(globals_, Mapping) and key in globals_:
        return globals_[key]
    if isinstance(config, Mapping) and key in config:
        return config[key]

    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, _CONFIG_UNSET)
        except TypeError:
            return _CONFIG_UNSET
    return _CONFIG_UNSET


def _normalize_level(value: Any) -> int:
    if isinstance(value, bool):
        raise ValidationError("log_level must be a logging level name or integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        level = logging.getLevelName(value.upper())
        if isinstance(level, int):
            return level
    raise ValidationError(f"invalid log_level: {value!r}")


def _normalize_setting(key: str, value: Any) -> Any:
    if key == "log_level":
        return _normalize_level(value)
    if key == "log_output":
        if not isinstance(value, str) or value not in {"builtin", "business", "both"}:
            raise ValidationError("log_output must be one of: builtin, business, both")
        return value
    if key in {"log_format", "log_datefmt"}:
        if not isinstance(value, str):
            raise ValidationError(f"{key} must be a string")
        return value
    if key == "log_file":
        if value is None or value == "":
            return ""
        if not isinstance(value, (str, os.PathLike)):
            raise ValidationError("log_file must be a string path")
        return os.fspath(value)
    raise AssertionError(f"unknown log setting: {key}")


def _normalize_log_filter_overrides(value: Any) -> dict[str, dict[str, str | None]]:
    """校验 ``globals.log_filters`` 结构，保留显式叶项以便进程级逐项比较。"""
    root_path = "globals.log_filters"
    if not isinstance(value, Mapping):
        raise ValidationError(f"{root_path} must be a mapping")

    overrides: dict[str, dict[str, str | None]] = {}
    for route in value:
        if route not in {"business", "builtin"}:
            raise ValidationError(f"unknown field at {root_path}")
        route_path = f"{root_path}.{route}"
        route_value = value[route]
        if not isinstance(route_value, Mapping):
            raise ValidationError(f"{route_path} must be a mapping")

        route_overrides: dict[str, str | None] = {}
        for field in route_value:
            if field not in {"include", "exclude"}:
                raise ValidationError(f"unknown field at {route_path}")
            field_path = f"{route_path}.{field}"
            expression = route_value[field]
            if expression is None:
                route_overrides[field] = None
            elif not isinstance(expression, str):
                raise ValidationError(f"{field_path} must be a string or null")
            elif expression == "":
                raise ValidationError(f"{field_path} must not be empty")
            else:
                route_overrides[field] = expression
        overrides[route] = route_overrides
    return overrides


def _merge_log_filter_overrides(
    overrides: dict[str, dict[str, str | None]],
) -> _LogFilters:
    """把首次配置的显式叶项合并到无过滤默认值。"""
    routes: dict[str, _LogFilterRules] = {}
    for route in ("business", "builtin"):
        values = overrides.get(route, {})
        routes[route] = _LogFilterRules(
            include=values.get("include"),
            exclude=values.get("exclude"),
        )
    return _LogFilters(business=routes["business"], builtin=routes["builtin"])


def _compile_log_filters(filters: _LogFilters) -> dict[str, _CompiledLogFilter]:
    """初始化时编译所有规则；相同原文在两路之间共用同一 Pattern。"""
    compiled_patterns: dict[str, re.Pattern[str]] = {}

    def compile_field(route: str, field: str, expression: str | None) -> re.Pattern[str] | None:
        if expression is None:
            return None
        pattern = compiled_patterns.get(expression)
        if pattern is not None:
            return pattern
        path = f"globals.log_filters.{route}.{field}"
        try:
            pattern = re.compile(expression)
        except (re.error, OverflowError, RecursionError, TypeError) as exc:
            position = getattr(exc, "pos", None)
            location = (
                f"position {position}"
                if isinstance(position, int)
                else "position unavailable"
            )
            raise ValidationError(
                f"invalid regular expression at {path} ({location})"
            ) from None
        compiled_patterns[expression] = pattern
        return pattern

    compiled: dict[str, _CompiledLogFilter] = {}
    for route in ("business", "builtin"):
        rules = getattr(filters, route)
        compiled[route] = _CompiledLogFilter(
            include=compile_field(route, "include", rules.include),
            exclude=compile_field(route, "exclude", rules.exclude),
        )
    return compiled


def _copy_containers(value: Any, memo: dict[int, Any] | None = None) -> Any:
    """复制 record 中的内置容器，保留容器外的业务对象身份。"""
    if memo is None:
        memo = {}
    identity = id(value)
    if identity in memo:
        return memo[identity]

    # Mapping/序列统一投影为对应内置容器：日志格式化与常见修改操作保持可用，
    # 同时不调用可能要求业务参数的自定义构造器，也不复制任意 opaque 对象。
    if isinstance(value, Mapping):
        cloned: dict[Any, Any] = {}
        memo[identity] = cloned
        items = dict.items(value) if isinstance(value, dict) else value.items()
        for key, item in items:
            cloned[key] = _copy_containers(item, memo)
        return cloned
    if isinstance(value, (list, UserList)):
        cloned_list: list[Any] = []
        memo[identity] = cloned_list
        source = value.data if isinstance(value, UserList) else value
        cloned_list.extend(_copy_containers(item, memo) for item in list.__iter__(source))
        return cloned_list
    if isinstance(value, deque):
        cloned_deque: deque[Any] = deque(maxlen=value.maxlen)
        memo[identity] = cloned_deque
        cloned_deque.extend(_copy_containers(item, memo) for item in deque.__iter__(value))
        return cloned_deque
    if isinstance(value, tuple):
        cloned_tuple = tuple(_copy_containers(item, memo) for item in value)
        memo[identity] = cloned_tuple
        return cloned_tuple
    if isinstance(value, set):
        cloned_set = {_copy_containers(item, memo) for item in set.__iter__(value)}
        memo[identity] = cloned_set
        return cloned_set
    if isinstance(value, frozenset):
        cloned_frozenset = frozenset(
            _copy_containers(item, memo) for item in frozenset.__iter__(value)
        )
        memo[identity] = cloned_frozenset
        return cloned_frozenset
    if isinstance(value, bytearray):
        cloned_bytes = bytearray(value)
        memo[identity] = cloned_bytes
        return cloned_bytes
    return value


def _copy_record(record: logging.LogRecord) -> logging.LogRecord:
    """复制 LogRecord 和其中的标准容器，不 deepcopy 任意业务对象或 traceback。"""
    copied = logging.makeLogRecord(record.__dict__)
    memo: dict[int, Any] = {}
    for key, value in record.__dict__.items():
        if key == "exc_info":
            copied.__dict__[key] = value
        else:
            copied.__dict__[key] = _copy_containers(value, memo)
    return copied


def _write_dispatch_failure(
    handler: logging.Handler,
    exception: Exception,
    *,
    operation: str,
) -> None:
    """以不含异常消息或源码行的格式直接报告单个输出目标故障。"""
    try:
        handler_name = type(handler).__name__
    except Exception:
        handler_name = "logging handler"
    write_diagnostic(handler_name, exception, operation=operation)


class _DispatchHandler(logging.Handler):
    """把一条已脱敏日志分发为各输出独立的 LogRecord 副本。"""

    def __init__(
        self,
        destinations: tuple[tuple[logging.Handler, _CompiledLogFilter], ...],
        level: int,
    ) -> None:
        super().__init__(level)
        self._destinations = destinations
        self._privacy_filter = SensitiveDataFilter()

    def handle(self, record: logging.LogRecord) -> bool:
        # 子 logger 可以单独降低级别；传播不会重查父 logger 的级别，故在此统一拦截。
        if record.levelno < self.level:
            return False
        # 不调用 Handler.handle：其锁会覆盖 emit 及业务回调，可能把所有产生日志的线程
        # 串行化。每个目标先检查级别，再通过自身 handle() 执行过滤和加锁。
        if getattr(_DISPATCH_LOCAL, "active", False):
            return False
        if not self.filter(record):
            return False
        _DISPATCH_LOCAL.active = True
        try:
            if not self._privacy_filter.filter(record):
                return False
            prepared_destinations: list[
                tuple[logging.Handler, logging.LogRecord | None]
            ] = []
            for destination, rules in self._destinations:
                if record.levelno < destination.level:
                    continue
                if rules.include is None and rules.exclude is None:
                    # 无规则路不准备正文，避免 getMessage() 失败连带丢掉这路日志。
                    prepared_destinations.append((destination, None))
                    continue
                try:
                    output_record = _copy_record(record)
                except Exception as exc:
                    _write_dispatch_failure(destination, exc, operation="record copy")
                    continue
                try:
                    message = output_record.getMessage()
                    if rules.exclude is not None and rules.exclude.search(message) is not None:
                        continue
                    if rules.include is not None and rules.include.search(message) is None:
                        continue
                except Exception as exc:
                    _write_dispatch_failure(destination, exc, operation="log filter match")
                    continue
                prepared_destinations.append((destination, output_record))

            # 在任何 Handler 回调前准备好所有带规则路的匹配决定。业务 Handler 即使改写
            # 自定义对象，也不能改变稍后内置输出所依据的正文快照。
            for destination, output_record in prepared_destinations:
                if output_record is None:
                    try:
                        output_record = _copy_record(record)
                    except Exception as exc:
                        _write_dispatch_failure(destination, exc, operation="record copy")
                        continue
                try:
                    destination.handle(output_record)
                except Exception as exc:
                    _write_dispatch_failure(destination, exc, operation="handler call")
            return True
        finally:
            _DISPATCH_LOCAL.active = False

    def emit(self, record: logging.LogRecord) -> None:
        """标准 Handler 抽象方法；实际分发由无共享锁的 ``handle`` 完成。"""
        del record


def _build_handlers(
    settings: _LogSettings,
    business_handler: logging.Handler | None,
    filters: dict[str, _CompiledLogFilter],
) -> tuple[_DispatchHandler, tuple[logging.Handler, ...]]:
    formatter = logging.Formatter(settings.log_format, datefmt=settings.datefmt)
    destinations: list[tuple[logging.Handler, _CompiledLogFilter]] = []
    owned_handlers: list[logging.Handler] = []
    original_formatter = business_handler.formatter if business_handler is not None else None
    try:
        if settings.output in {"business", "both"}:
            if business_handler is None:
                raise ValidationError(
                    f"log_output={settings.output!r} requires a log_handler"
                )
            if business_handler.formatter is None:
                business_handler.setFormatter(formatter)
            destinations.append((business_handler, filters["business"]))

        if settings.output in {"builtin", "both"}:
            stream_handler = logging.StreamHandler()
            stream_handler.setLevel(settings.level)
            stream_handler.setFormatter(formatter)
            install_privacy_filter(stream_handler)
            destinations.append((stream_handler, filters["builtin"]))
            owned_handlers.append(stream_handler)

            if settings.log_file:
                os.makedirs(os.path.dirname(settings.log_file) or ".", exist_ok=True)
                file_handler = logging.FileHandler(settings.log_file, encoding="utf-8")
                owned_handlers.append(file_handler)
                file_handler.setLevel(settings.level)
                file_handler.setFormatter(formatter)
                install_privacy_filter(file_handler)
                destinations.append((file_handler, filters["builtin"]))

        return _DispatchHandler(tuple(destinations), settings.level), tuple(owned_handlers)
    except BaseException:
        for owned_handler in owned_handlers:
            owned_handler.close()
        if business_handler is not None and business_handler.formatter is not original_formatter:
            try:
                business_handler.setFormatter(original_formatter)
            except Exception as restore_error:
                _write_dispatch_failure(
                    business_handler, restore_error, operation="formatter restore"
                )
        raise


def _set_router_handlers(
    logger: logging.Logger, router: _DispatchHandler, *, install: bool
) -> None:
    """与标准增删 Handler 共用锁，发布新列表，保留正在遍历的旧列表。"""
    # logging 没有用于前置安装的公共锁接口；只在替换列表时使用模块锁，不调用业务处理器。
    with logging._lock:  # pylint: disable=protected-access
        remaining = [handler for handler in logger.handlers if handler is not router]
        logger.handlers = [router, *remaining] if install else remaining


def _install_router(router: _DispatchHandler, level: int) -> list[tuple[logging.Logger, int, bool]]:
    """接管两个 SDK 根命名空间，同时保留其已有命名空间 Handler。"""
    snapshots: list[tuple[logging.Logger, int, bool]] = []
    try:
        for name in _SDK_LOG_ROOTS:
            logger = logging.getLogger(name)
            snapshots.append((logger, logger.level, logger.propagate))
            logger.setLevel(level)
            logger.propagate = False
            # 保留 SDK 先分发独立副本的顺序，但不就地重排正在被其他线程遍历的列表。
            _set_router_handlers(logger, router, install=True)
        return snapshots
    except BaseException:
        for logger, old_level, old_propagate in snapshots:
            _set_router_handlers(logger, router, install=False)
            logger.setLevel(old_level)
            logger.propagate = old_propagate
        raise


def _restore_installation(
    router: _DispatchHandler,
    snapshots: list[tuple[logging.Logger, int, bool]],
) -> None:
    for logger, old_level, old_propagate in snapshots:
        _set_router_handlers(logger, router, install=False)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def setup_logging(config=None, *, log_handler: logging.Handler | None = None) -> None:
    """一次性配置 SDK logger；后续装配只能省略或复用同一组日志设置。

    ``log_level`` / ``log_format`` / ``log_datefmt`` / ``log_file`` / ``log_output`` 与
    ``log_filters`` 从 ``globals`` 读取。输出为 ``builtin`` 时写 stderr 与可选 UTF-8
    追加文件；``business`` 写入调用方传入的 Handler；``both`` 同时写入。没有显式输出模式时，
    首次装配始终默认使用 ``builtin``；传入 ``log_handler`` 不会自动启用业务输出。
    """
    explicit = {}
    for key in ("log_level", "log_format", "log_datefmt", "log_file", "log_output"):
        value = _config_value(config, key)
        if value is not _CONFIG_UNSET:
            explicit[key] = _normalize_setting(key, value)
    raw_filters = _config_value(config, "log_filters")
    if raw_filters is not _CONFIG_UNSET:
        explicit["log_filters"] = _normalize_log_filter_overrides(raw_filters)

    global _LOGGING_STATE
    with _STATE_LOCK:
        if _LOGGING_STATE is not None:
            state = _LOGGING_STATE
            for key, value in explicit.items():
                if key == "log_filters":
                    for route, fields in value.items():
                        existing_rules = getattr(state.settings.filters, route)
                        for field, expression in fields.items():
                            if expression != getattr(existing_rules, field):
                                raise ValidationError(
                                    "logging configuration conflict for "
                                    f"globals.log_filters.{route}.{field}: "
                                    "the process already uses a different setting"
                                )
                    continue
                setting_name = {
                    "log_level": "level",
                    "log_format": "log_format",
                    "log_datefmt": "datefmt",
                    "log_file": "log_file",
                    "log_output": "output",
                }[key]
                if value != getattr(state.settings, setting_name):
                    raise ValidationError(
                        f"logging configuration conflict for {key}: "
                        "the process already uses a different setting"
                    )
            if log_handler is not None and log_handler is not state.business_handler:
                raise ValidationError(
                    "logging configuration conflict for log_handler: "
                    "the process already uses a different handler"
                )
            return

        settings = _LogSettings(
            level=explicit.get("log_level", logging.INFO),
            log_format=explicit.get("log_format", _DEFAULT_FORMAT),
            datefmt=explicit.get("log_datefmt", _DEFAULT_DATEFMT),
            log_file=explicit.get("log_file", ""),
            output=explicit.get("log_output", "builtin"),
            filters=_merge_log_filter_overrides(explicit.get("log_filters", {})),
        )
        if settings.output in {"business", "both"} and log_handler is None:
            raise ValidationError(
                f"log_output={settings.output!r} requires a log_handler"
            )

        original_formatter = log_handler.formatter if log_handler is not None else None
        router: _DispatchHandler | None = None
        owned_handlers: tuple[logging.Handler, ...] = ()
        snapshots: list[tuple[logging.Logger, int, bool]] = []
        try:
            compiled_filters = _compile_log_filters(settings.filters)
            router, owned_handlers = _build_handlers(
                settings, log_handler, compiled_filters
            )
            snapshots = _install_router(router, settings.level)
            _LOGGING_STATE = _LoggingState(settings, log_handler, router, owned_handlers)
        except BaseException:
            if snapshots and router is not None:
                _restore_installation(router, snapshots)
            if router is not None:
                for owned_handler in owned_handlers:
                    owned_handler.close()
            if log_handler is not None and log_handler.formatter is not original_formatter:
                try:
                    log_handler.setFormatter(original_formatter)
                except Exception as restore_error:
                    _write_dispatch_failure(
                        log_handler, restore_error, operation="formatter restore"
                    )
            raise
