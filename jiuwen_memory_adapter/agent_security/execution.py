"""最终发送边界的共用门禁；框架只负责可信调用关联和原生审批控制流。"""

from __future__ import annotations

import inspect
import json
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from ._validation import has_exact_type
from .config import AgentSecurityConfig
from .frameworks import load_runtime_driver
from .hooks import Hook
from .runtime import ApprovalChoice, SecurityDenied, encode_payload


@dataclass(frozen=True)
class ExecutionContext:
    """host 来自宿主回调，不来自工具参数；operation_for 仍须解析当前认证身份。"""

    framework: str
    name: str
    call_id: str
    host: object = field(repr=False)


@dataclass
class _Frame:
    context: ExecutionContext
    used: bool = False
    pending: object = None
    reply: object = None
    initial_json: str | None = field(default=None, repr=False)
    token: object = None


@dataclass
class _Execution:
    operation: object = field(repr=False)
    request: object
    revision: str
    consumed: bool = False


def _object(value):
    if not has_exact_type(value, dict):
        raise SecurityDenied("执行参数必须是 JSON 对象")
    return json.loads(encode_payload(value))


def _freeze(value):
    if has_exact_type(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if has_exact_type(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _choice(raw, request):
    if (
        not has_exact_type(raw, dict)
        or set(raw) != {"approval_id", "version", "choice"}
        or raw["approval_id"] != request.approval_id
    ):
        raise SecurityDenied("审批必须引用精确的请求版本")
    elif raw["version"] != request.version:
        raise SecurityDenied("审批必须引用精确的请求版本")
    try:
        return ApprovalChoice(raw["choice"])
    except (ValueError, TypeError):
        raise SecurityDenied("无效的审批选项") from None


class ExecutionAdapter(ABC):
    """实例内有界、单次执行。工具提供方只向 send 暴露最终批准请求。"""

    framework: str

    @abstractmethod
    def create_binding(self):
        """返回本框架原生 Rail/middleware；宿主须显式装入工具调用路径。"""

    @abstractmethod
    def create_tool(self, *, name, description, input_schema, send, prepare_request=None):
        """构造原生受保护工具；send 只接收批准请求和可信 ExecutionContext。"""

    def __init__(
        self, guard, *, operation_for, capacity=1024, sources=None, results=None, audit=None
    ):
        if not has_exact_type(capacity, int) or capacity < 1:
            raise ValueError("capacity 必须是正整数")
        self.guard, self.operation_for, self.capacity = guard, operation_for, capacity
        self.sources = sources
        self.results, self.audit = results, audit
        self._frames = ContextVar(f"jiuwen_execution_{id(self)}", default=None)
        self._entries = {}
        self._names = set()
        self._closed = False
        self._lock = threading.RLock()

    def _register(self, name):
        with self._lock:
            if self._closed or not isinstance(name, str) or (not name):
                raise SecurityDenied("工具重复注册或执行适配器已关闭")
            elif name in self._names or len(self._names) >= self.capacity:
                raise SecurityDenied("工具重复注册或执行适配器已关闭")
            self._names.add(name)

    def _frame(self, name):
        frame = self._frames.get()
        if self._closed or frame is None or frame.context.name != name:
            raise SecurityDenied("缺少可信工具调用关联或执行凭据已使用")
        elif frame.used:
            raise SecurityDenied("缺少可信工具调用关联或执行凭据已使用")
        frame.used = True
        return frame

    def _operation(self, context, payload):
        operation = self.operation_for(context, _object(payload))
        if (
            operation.hook != Hook.BEFORE_TOOL_CALL
            or operation.action != context.name
            or operation.operation_id != context.call_id
        ):
            raise SecurityDenied("宿主执行关联不匹配")
        elif operation.payload_json != encode_payload(payload):
            raise SecurityDenied("宿主执行关联不匹配")
        if self.sources is not None:
            snapshot = self.sources.snapshot(operation.actor)
            operation = replace(
                operation, source_level=snapshot.level, source_version=snapshot.version
            )
        return operation

    def _recheck(self, frame, entry):
        current = self._operation(frame.context, entry.operation.payload)
        self._authorize(current)
        if current != entry.operation or self.guard.revision != entry.revision:
            raise SecurityDenied("发送前操作或来源发生变化")

    @contextmanager
    def _audit_sending(self, operation):
        def emit(outcome):
            if self.audit is not None:
                self.audit.emit(
                    actor=operation.actor,
                    hook=operation.hook,
                    operation_id=operation.operation_id,
                    target=operation.target,
                    level=operation.source_level,
                    outcome=outcome,
                )

        emit("started")
        try:
            yield
        except BaseException:
            emit("unknown")
            raise
        else:
            emit("completed")

    @asynccontextmanager
    async def _asending(self, frame, entry):
        if self.sources is None:
            self._recheck(frame, entry)
            with self._audit_sending(entry.operation):
                yield
        else:
            async with self.sources.alease(entry.operation.actor, entry.operation.source_version):
                self._recheck(frame, entry)
                with self._audit_sending(entry.operation):
                    yield

    def _authorize(self, operation):
        if (
            self._closed
            or self.guard.authorize(operation) is not True
            or operation.target not in self.guard.policy.allowed_targets
        ):
            raise SecurityDenied("当前主体无权执行该操作")

    def _before_prepare(self, frame, payload):
        operation = self._operation(frame.context, payload)
        self._authorize(operation)
        return operation

    def _prepared(self, frame, preliminary, prepared):
        operation = self._operation(frame.context, _object(prepared))
        if (operation.actor, operation.target) != (preliminary.actor, preliminary.target):
            raise SecurityDenied("准备请求时身份或目标改变")
        key = (operation.actor, operation.hook, operation.operation_id)
        with self._lock:
            self._authorize(operation)
            previous = self._entries.get(key)
            if previous:
                if (
                    previous.consumed
                    or previous.operation != operation
                    or previous.revision != self.guard.revision
                ):
                    self._cancel(previous)
                    raise SecurityDenied("最终请求已改变或调用已结束")
                # 同一原生 continuation 重放准备步骤，只复用相同最终请求。
                self.guard.prepare(operation)
                return previous
            if len(self._entries) >= self.capacity:
                raise SecurityDenied("执行容量已满，请轮换会话实例")
            pending = self.guard.prepare(operation)
            entry = _Execution(operation, pending, self.guard.revision)
            self._entries[key] = entry
            return entry

    async def _aprepare(self, frame, payload, prepare_request):
        payload = _object(payload)
        preliminary = self._before_prepare(frame, payload)
        prepared = prepare_request(_freeze(payload), frame.context) if prepare_request else payload
        if inspect.isawaitable(prepared):
            prepared = await prepared
        return self._prepared(frame, preliminary, prepared)

    def _select(self, frame, entry, raw=None):
        with self._lock:
            if entry.consumed or self._closed:
                raise SecurityDenied("该执行已结束")
            current = self._operation(frame.context, entry.operation.payload)
            self._authorize(current)
            if current != entry.operation or self.guard.revision != entry.revision:
                self._cancel(entry)
                raise SecurityDenied("审批后身份、来源、策略或目标改变")
            if entry.request is not None:
                choice = _choice(raw, entry.request)
                self.guard.broker.submit(
                    entry.request.approval_id,
                    entry.request.version,
                    choice,
                    reviewer=current.actor,
                )
                selected = self.guard.claim(entry.request, current)
            else:
                selected = entry.operation.payload
            entry.consumed = True
            return _freeze(selected)

    def _cancel(self, entry):
        with self._lock:
            entry.consumed = True
            if entry.request is not None:
                self.guard.broker.cancel(entry.request.approval_id)

    async def _aresult(self, entry, value):
        if self.results is None:
            return value
        return await self.results.aobserve(
            entry.operation.actor,
            hook=Hook.AFTER_TOOL_CALL,
            producer=entry.operation.action,
            operation_id=entry.operation.operation_id,
            value=value,
        )

    def close(self):
        """关闭包装，不关闭宿主可能共享的 Broker。已开始的外部副作用不能撤回。"""
        with self._lock:
            self._closed = True
            for entry in self._entries.values():
                self._cancel(entry)
            self._entries.clear()
            self._names.clear()


def create_execution_adapter(
    config: AgentSecurityConfig | Mapping[str, object],
    guard,
    *,
    operation_for,
    capacity=1024,
    sources=None,
    results=None,
    audit=None,
    driver=None,
    runtime_registry=None,
) -> ExecutionAdapter:
    """沿用 framework 选型；只选择显式运行时驱动，不从静态映射推断实现。"""
    if not isinstance(config, AgentSecurityConfig):
        config = AgentSecurityConfig.from_mapping(config)
    selected = (
        driver if driver is not None else load_runtime_driver(config, registry=runtime_registry)
    )
    if selected.framework != config.framework:
        raise SecurityDenied("运行时驱动与框架配置不匹配")
    return selected.create_execution(
        guard,
        operation_for=operation_for,
        capacity=capacity,
        sources=sources,
        results=results,
        audit=audit,
    )
