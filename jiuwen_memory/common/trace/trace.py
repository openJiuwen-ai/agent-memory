# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TraceCollector — 请求级处理过程追踪（横切共用组件）。

记录一次请求的处理过程，存储为**调用树**：节点由「组件（模块名）→ 执行阶段」两级
命名，span 节点包住一段执行并计时、其内的事件与子 span 挂为 children。
与 :mod:`common.audit` 类似，trace 是横切基础设施而非可插拔计算能力，
不继承 Plugin、不进 PluginType，单独成一类横切组件。

**零传参使用方式**：业务代码不持有、不接收任何 trace 实例，import 模块级
函数直接调用::

    from jiuwen_memory.common.trace import trace_event, trace_span, traced

    @traced("write")                       # 方法 span，component 取 type(self).__name__
    async def write(self, ...): ...

    with trace_span("PipelineRetriever", "retrieve"):
        trace_event("PipelineRetriever", "parse", {"candidates": 3, "cost_ms": 1.2})

无活动请求作用域（或作用域为 noop）时静默返回（一次 ContextVar.get 的开销），
未埋点的模块完全无感。

**详细程度**：三档 :class:`TraceLevel`。``simple`` 记关键计数与耗时；
``info``（缺省）增加阶段/路径；
``debug`` 记逐条明细（如每个候选的去重决定），埋点处以 ``level=TraceLevel.DEBUG``
声明。作用域阈值低于事件级别时在拷贝数据前丢弃，概要模式下明细埋点近乎零开销。
阈值来源：请求覆盖 ``extensions["trace"] = "<target>[:<level>]"`` > 实现的
``params.level``（config）> ``info``。

**请求作用域**：入口层（``jiuwen_memory_entry/core/api_contract.invoke_api``，
HTTP/CLI/MCP 三端共用；legacy ``handler.dispatch`` 同口径）经
:func:`begin_trace` / :func:`end_trace` 包住一次请求；请求级数据存于 ContextVar
携带的 ``_TraceRequest`` 容器，并发请求互不串数据；实现类不保存请求状态，
可持有配置与线程安全 sink 资源。
当前 span 另存一个 ContextVar：``asyncio.gather``/``create_task`` 复制 context，
并发子任务各自挂在发起时的父 span 下，不会互相嵌套。

**trace_id**：取当前认证请求的 request_id（``common.security.request_context``），
日志行与响应 snapshot 都带上，可与审计/访问日志关联；无 request_id 时现生成。

**实现选择**：

- config 默认实现：``trace.default.target`` ∈ {noop, memory, log, memory_log}，
  装配期经 :func:`configure_default` 注入；缺省 noop，noop 作用域等同无作用域。
- 请求级覆盖：请求 ``extensions["trace"]`` 指定 ``<target>[:<level>]``（target 可空，
  如 ``":debug"`` 表示沿用默认实现只提高详细程度），由入口层在参数校验前剥离，
  仅作诊断开关，不参与鉴权/路由；非法值 fail-fast 抛 ``ValidationError``。

**隐私约定**：memory buffer 数据直接进 response（返回给发起者本人），不做审查；
log sink 走统一日志命名空间，正文/查询等字段使用隐私标记；凭证不得进入埋点。
"""

from __future__ import annotations

import functools
import inspect
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from threading import RLock
from time import perf_counter
from typing import Any

from ..errors import ValidationError
from ..factory.factory import Factory
from ..security.request_context import get_request_id

# --------------------------------------------------------------------------- #
# 体积护栏：单条字符串截断 / 单请求节点数上限。超限降级为计数标记，不静默丢。
# --------------------------------------------------------------------------- #
_TRUNCATED = "...[truncated]"

TRACE_OVERRIDE_KEY = "trace"  # extensions 里的请求级覆盖键


class TraceLevel(str, Enum):
    """详细程度递增：simple 关键统计、info 阶段信息、debug 逐条明细。"""

    SIMPLE = "simple"
    INFO = "info"
    DEBUG = "debug"


def parse_level(value: Any) -> TraceLevel:
    """把配置/请求里的级别值解析为 :class:`TraceLevel`；非法值抛 ValidationError。"""
    if isinstance(value, TraceLevel):
        return value
    if isinstance(value, str):
        try:
            return TraceLevel(value.strip().lower())
        except ValueError:
            pass
    allowed = ", ".join(level.value for level in TraceLevel)
    raise ValidationError(f"trace level 只能是 {allowed}，得到 {value!r}")


def parse_trace_spec(spec: str) -> tuple[str | None, TraceLevel | None]:
    """解析请求级覆盖 ``<target>[:<level>]``；空 target 表示沿用默认实现。"""
    target, sep, level = spec.partition(":")
    target = target.strip()
    return (target or None), (parse_level(level) if sep else None)


def _bound_value(value: Any, limits: TraceLimits, depth: int = 0) -> Any:
    """复制记录并统一裁剪；过滤完成后才求值惰性 data。"""
    if isinstance(value, Enum):
        return _bound_value(value.value, limits, depth)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str):
        maximum = limits.max_value_chars
        return value if maximum is None or len(value) <= maximum else value[:maximum] + _TRUNCATED
    if (limits.max_depth is not None and depth >= limits.max_depth
            and isinstance(value, (Mapping, list, tuple, set, frozenset))):
        return f"<{type(value).__name__}>"
    if isinstance(value, Mapping):
        return {str(k): _bound_value(v, limits, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_bound_value(v, limits, depth + 1) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


@dataclass(frozen=True)
class TraceLimits:
    """一次执行的体积策略；None 取消对应限制。"""

    max_nodes: int | None = 128
    max_value_chars: int | None = 256
    max_depth: int | None = 4

    def __post_init__(self) -> None:
        for name in ("max_nodes", "max_value_chars", "max_depth"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValidationError(f"trace_{name} 必须是正整数或 null")


class TraceCollector(ABC):
    """横切 trace 组件契约。

    实现不保存请求状态：请求级调用树由框架维护，实例可持有配置和线程安全 sink 资源。
    :meth:`record` 定义输出，实例被本次装配的请求共享。
    """

    #: 是否收集调用树（供 response 填充）。memory/memory_log 为 True。
    buffers: bool = False
    #: False 时 begin_trace 不开作用域，埋点全部短路（noop）。
    enabled: bool = True

    def __init__(self, level: TraceLevel = TraceLevel.INFO) -> None:
        #: 作用域缺省阈值（config ``params.level``）；装配期确定，不随请求变化。
        self.level = parse_level(level)

    def close(self) -> None:
        """运行时在后台任务收尾后释放 sink 资源；无资源插件沿用 no-op。"""

    @abstractmethod
    def record(self, trace_id: str, path: str, data: Mapping[str, Any], level: TraceLevel) -> None:
        """sink 行为。``path`` 形如 ``Engine.write/Extractor.extract``（根到本节点）。

        事件在记录时调用一次；span 在退出时调用一次，data 含 ``cost_ms``。
        只收到不低于作用域阈值的节点。
        """


class TraceProducer(Factory):
    """TraceCollector 的注册式工厂（与契约同处接口层）。

    各实现在 ``trace_impl`` 下以 ``@TraceProducer.register("<名>")`` 自注册，
    由 :func:`common.bootstrap.register_plugins` 统一触发。
    """

    TOP_NAME = "trace"


def level_param(config: Any) -> TraceLevel:
    """builder 读取 ``params.level``（缺省 info）。

    只读本实例 params、不回退 globals：``level`` 不是全局参数，且 resolve_target
    以 ``ctx=None`` 构建请求级覆盖实例。
    """
    return parse_level(config.params.get("level", TraceLevel.INFO.value))


# --------------------------------------------------------------------------- #
# 请求作用域与调用树
# --------------------------------------------------------------------------- #


@dataclass
class _TraceRequest:
    """一次请求的 trace 容器：实现引用 + 调用树根节点列表。"""

    collector: TraceCollector
    trace_id: str
    level: TraceLevel = TraceLevel.INFO
    limits: TraceLimits = field(default_factory=TraceLimits)
    lock: Any = field(default_factory=RLock, repr=False)
    roots: list[_Node] = field(default_factory=list)
    nodes: int = 0
    dropped: int = 0
    #: end_trace 后置 True。后台任务（调度器 drain/定时任务）创建时会复制当时的
    #: context，持有已结束请求的容器；关闭后其事件一律丢弃，不串入他人请求的日志。
    closed: bool = False


class _Node:
    """调用树节点。span 节点有 ``cost_ms``/``children``，事件节点只有 data。"""

    __slots__ = (
        "owner",
        "path",
        "component",
        "stage",
        "level",
        "data",
        "children",
        "cost_ms",
        "error",
        "retained",
    )

    def __init__(
        self, owner: _TraceRequest, path: str, component: str, stage: str, level: TraceLevel
    ) -> None:
        self.owner = owner
        self.path = path
        self.component = component
        self.stage = stage
        self.level = parse_level(level)
        self.data: dict[str, Any] = {}
        self.children: list[_Node] | None = None
        self.cost_ms: float | None = None
        self.error: str | None = None
        self.retained = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"component": self.component, "stage": self.stage}
        if self.level != TraceLevel.INFO:
            out["level"] = self.level.value
        if self.data:
            out["data"] = self.data
        if self.cost_ms is not None:
            out["cost_ms"] = self.cost_ms
        if self.error is not None:
            out["error"] = self.error
        if self.children:
            out["children"] = [child.to_dict() for child in self.children]
        return out


_CURRENT: ContextVar[_TraceRequest | None] = ContextVar("agent_memory_trace", default=None)
_SCOPE_LIMITS: ContextVar[TraceLimits | None] = ContextVar("trace_limits", default=None)


@dataclass(frozen=True)
class TraceToken:
    """成对恢复容器和执行策略的 opaque token。"""

    request: Token[_TraceRequest | None]
    limits: Token[TraceLimits | None]

_SPAN: ContextVar[_Node | None] = ContextVar("agent_memory_trace_span", default=None)


class _NoopDefault(TraceCollector):
    """未装配时的默认实现：不开作用域、不落任何 sink。"""

    enabled = False

    def record(self, trace_id: str, path: str, data: Mapping[str, Any], level: TraceLevel) -> None:
        return None


def noop_collector() -> TraceCollector:
    """创建禁用采集的 collector；不读取配置，也不依赖插件注册。"""
    return _NoopDefault()


_DEFAULT: TraceCollector = noop_collector()
_TARGETS: dict[str, tuple[object, TraceCollector]] = {}
_TARGET_LOCK = RLock()
_LIMITS = TraceLimits()


def configure_default(collector: TraceCollector, *, limits: TraceLimits | None = None) -> None:
    """装配期注入 config 指定的默认实现（每次 assemble 覆盖，测试间须重建）。"""
    global _DEFAULT, _LIMITS
    if not isinstance(collector, TraceCollector):
        raise ValidationError(f"trace 默认实现不是 TraceCollector: {type(collector).__name__}")
    _DEFAULT = collector
    _LIMITS = limits or TraceLimits()


def resolve_target(target: str) -> TraceCollector:
    """按 target 名取共享 sink 实例（按注册 builder 缓存）；未注册名抛 ValidationError。"""
    # Factory 注册可以替换；缓存必须与当前 builder 绑定，不能复用旧插件。
    with _TARGET_LOCK:
        builder = TraceProducer.registration_key(target)
        cached = _TARGETS.get(target)
        if cached is not None and cached[0] is builder:
            return cached[1]
        instance = TraceProducer.build(target, {}, ctx=None)
        if not isinstance(instance, TraceCollector):
            raise ValidationError(f"trace target {target!r} 未产出 TraceCollector")
        _TARGETS[target] = (builder, instance)
        return instance


def begin_trace(
    target: str | None = None,
    trace_id: str | None = None,
    *,
    level: TraceLevel | str | None = None,
    collector: TraceCollector | None = None,
    limits: TraceLimits | None = None,
) -> TraceToken:
    """开启本请求的 trace 作用域。

    ``target`` 缺省用 config 默认实现；``level`` 缺省用该实现的 ``params.level``。
    collector 未启用（noop）时把作用域置空：埋点调用与无作用域同价，
    同时屏蔽外层作用域，保证「本请求关闭 trace」语义。
    """
    if target is not None and collector is not None:
        raise ValidationError("begin_trace 的 target 与 collector 互斥")
    collector = collector if collector is not None else (
        resolve_target(target) if target else _DEFAULT
    )
    if not isinstance(collector, TraceCollector):
        raise ValidationError("trace collector 必须是 TraceCollector")
    threshold = parse_level(level if level is not None else collector.level)
    execution_limits = limits or _LIMITS
    policy_token = _SCOPE_LIMITS.set(execution_limits)
    if not collector.enabled:
        return TraceToken(_CURRENT.set(None), policy_token)
    request = _TraceRequest(
        collector=collector,
        trace_id=trace_id or get_request_id() or uuid.uuid4().hex,
        level=parse_level(threshold),
        limits=execution_limits,
    )
    return TraceToken(_CURRENT.set(request), policy_token)


def end_trace(token: TraceToken) -> dict[str, Any]:
    """结束作用域并返回收集到的调用树；无 buffer 语义或无节点时返回 {}。

    返回形如 ``{"trace_id": ..., "spans": [节点...], "dropped": n}``，
    ``dropped`` 仅在超出节点上限时出现。
    """
    request = _CURRENT.get()
    _CURRENT.reset(token.request)
    _SCOPE_LIMITS.reset(token.limits)
    if request is None:
        return {}
    with request.lock:
        request.closed = True
        if not request.collector.buffers or (not request.roots and not request.dropped):
            return {}
        snapshot: dict[str, Any] = {
            "trace_id": request.trace_id,
            "spans": [node.to_dict() for node in request.roots],
        }
        if request.dropped:
            snapshot["dropped"] = request.dropped
        return snapshot


def _attach(
    request: _TraceRequest, component: str, stage: str, level: TraceLevel, *, visible: bool = True
) -> _Node:
    """在当前 span 下新建节点；超出节点上限时返回不挂树的游离节点并计数。"""
    parent = _SPAN.get()
    if parent is not None and parent.owner is not request:
        parent = None  # 上一请求遗留的 span（复制来的 context），不跨请求挂载
    name = f"{component}.{stage}"
    path = f"{parent.path}/{name}" if parent is not None else name
    node = _Node(request, path, component, stage, level)
    captures = tuple(c for c in _CAPTURES.get() if c.request is request and not c.closed)
    if not request.collector.buffers and not captures:
        return node
    with request.lock:
        if request.closed:
            return node
        if request.limits.max_nodes is not None and request.nodes >= request.limits.max_nodes:
            request.dropped += 1
            for capture in captures:
                capture.dropped_nodes += 1
            return node
        request.nodes += 1
        node.retained = True
        if request.collector.buffers and visible:
            if parent is None:
                request.roots.append(node)
            else:
                if parent.children is None:
                    parent.children = []
                parent.children.append(node)
    return node


def _active(level: TraceLevel = TraceLevel.INFO) -> _TraceRequest | None:
    """取活动作用域；已关闭或阈值低于 ``level``（明细埋点 + 概要作用域）时返回 None。"""
    request = _CURRENT.get()
    if request is None or request.closed or not request.collector.enabled:
        return None
    if _LEVEL_ORDER[level] > _LEVEL_ORDER[request.level]:
        return None
    return request


_LEVEL_ORDER = {TraceLevel.SIMPLE: 0, TraceLevel.INFO: 1, TraceLevel.DEBUG: 2}
TraceData = Mapping[str, Any] | Callable[[], Mapping[str, Any]]


def trace_enabled(level: TraceLevel = TraceLevel.INFO) -> bool:
    """判断本次执行是否采集指定明细，供昂贵数据构造前短路。"""
    return _active(level) is not None


@dataclass
class TraceCapture:
    """局部阶段投影；不关闭外层请求，也不改变通用响应策略。"""

    request: _TraceRequest
    records: list[dict[str, Any]] = field(default_factory=list)
    dropped_nodes: int = 0
    closed: bool = False

    @property
    def dropped(self) -> int:
        return self.dropped_nodes


_CAPTURES: ContextVar[tuple[TraceCapture, ...]] = ContextVar("trace_captures", default=())


def current_capture() -> TraceCapture | None:
    request = _CURRENT.get()
    return next(
        (c for c in reversed(_CAPTURES.get()) if c.request is request and not c.closed), None
    )


@contextmanager
def capture_trace() -> Iterator[TraceCapture]:
    request = _CURRENT.get()
    temporary = request is None or request.closed
    token = None
    if temporary:
        request = _TraceRequest(
            noop_collector(), get_request_id() or uuid.uuid4().hex,
            limits=_SCOPE_LIMITS.get() or _LIMITS
        )
        token = _CURRENT.set(request)
    capture = TraceCapture(request)
    capture_token = _CAPTURES.set((*_CAPTURES.get(), capture))
    try:
        yield capture
    finally:
        with request.lock:
            capture.closed = True
            if token is not None:
                request.closed = True
        _CAPTURES.reset(capture_token)
        if token is not None:
            _CURRENT.reset(token)


def trace_event(
    component: str,
    stage: str,
    data: TraceData,
    *,
    level: TraceLevel = TraceLevel.INFO,
    projection: Mapping[str, Any] | None = None,
) -> None:
    """在当前 span 下记录一条事件；无活动作用域或低于阈值时静默返回。

    ``component`` 是模块名（通常 ``type(self).__name__``），``stage`` 是该模块内的
    执行阶段；同名事件按发生顺序逐条保留。逐条明细用 ``level=TraceLevel.DEBUG``。
    """
    active = _active(level)
    request = active or _CURRENT.get()
    if request is None or request.closed:
        return
    captures = tuple(c for c in _CAPTURES.get() if c.request is request and not c.closed)
    if active is None and not (projection is not None and captures):
        return
    with request.lock:
        if request.closed:
            return
        node = _attach(request, component, stage, level, visible=active is not None)
        if projection is not None and node.retained:
            # 保留独立字段及原精度 cost_ms，不从格式化 data 反推。
            bounded = {**projection, "detail": _bound_value(projection.get("detail", {}),
                                                          request.limits)}
            for capture in captures:
                if not capture.closed:
                    capture.records.append(bounded)
        if active is not None:
            node.data = _bound_value(dict(data() if callable(data) else data), request.limits)
            request.collector.record(request.trace_id, node.path, node.data, level)


def trace_annotate(data: Mapping[str, Any], *, level: TraceLevel = TraceLevel.INFO) -> None:
    """把 data 合并进当前 span（如 span 结束前补结果计数）；无 span 或低于阈值时静默返回。"""
    request = _active(level)
    span = _SPAN.get()
    if request is None or span is None or span.owner is not request:
        return
    with request.lock:
        if request.closed:
            return
        span.data.update(_bound_value(dict(data), request.limits))


@contextmanager
def trace_span(
    component: str,
    stage: str,
    data: Mapping[str, Any] | None = None,
    *,
    level: TraceLevel = TraceLevel.INFO,
) -> Iterator[None]:
    """包住一段执行：其内的事件/子 span 挂为本节点 children，退出时记录 ``cost_ms``。

    异常照常抛出，节点记 ``error`` 为异常类型名（不记异常文本，避免带入正文）。
    低于阈值的 span 不建节点，其内事件挂到外层 span。
    """
    request = _active(level)
    if request is None:
        yield
        return
    node = _attach(request, component, stage, level)
    if data:
        node.data = _bound_value(dict(data), request.limits)
    token = _SPAN.set(node)
    start = perf_counter()
    try:
        yield
    except BaseException as exc:
        node.error = type(exc).__name__
        raise
    finally:
        _SPAN.reset(token)
        with request.lock:
            if not request.closed:
                node.cost_ms = round((perf_counter() - start) * 1000.0, 3)
                log_data = {**node.data, "cost_ms": node.cost_ms}
                if node.error is not None:
                    log_data["error"] = node.error
                request.collector.record(request.trace_id, node.path, log_data, level)


def traced(
    stage: str, component: str | None = None, *, level: TraceLevel = TraceLevel.INFO
) -> Callable[[Callable], Callable]:
    """方法装饰器：整个调用包成一个 span，同步/异步方法均可。

    ``component`` 缺省取 ``type(self).__name__``（子类自动带出实现名）。
    无活动作用域或低于阈值时直接调用原函数，不进 contextmanager。
    """

    def decorate(fn: Callable) -> Callable:
        def name_of(args: tuple) -> str:
            return component or (type(args[0]).__name__ if args else fn.__module__)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if _active(level) is None:
                    return await fn(*args, **kwargs)
                with trace_span(name_of(args), stage, level=level):
                    return await fn(*args, **kwargs)

            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _active(level) is None:
                return fn(*args, **kwargs)
            with trace_span(name_of(args), stage, level=level):
                return fn(*args, **kwargs)

        return wrapper

    return decorate
