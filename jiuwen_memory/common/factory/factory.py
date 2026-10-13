# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Factory — 注册式工厂基类（开闭：分发封闭、实现可插拔）+ 具名实例共享。

每个 producer 继承本类，声明全局唯一的 ``TOP_NAME``（配置里的顶层命名空间），
并得到**独立的** ``target → builder`` 注册表；具名实例按上下文与 Producer 隔离。
具体实现用 ``@SomeProducer.register("实现名")``
把一个 ``_build(config) -> 实例`` 注册进来（接口 1 匿名创建的实现体）。在此之上：

- :meth:`build`（接口 1，匿名）：按 ``target`` 新建、不入缓存。
- :meth:`build_named`（接口 2，具名/共享）：按具名实例名经 ``ctx.lookup(TOP_NAME, name)`` 取配置、
  新建并按 ``new_instance`` 决定是否入缓存共享。
- :meth:`dep`：builder 内取一个依赖，按字段值
  「引用(str)→共享 / 内联(dict)→匿名 / 缺省→匿名」分派。
- ``config``：传给 ``_build`` 的 :class:`~config.context.ComponentConfig`——读参数
  ``config.get(...)``（本实例 params 回退 globals）、取依赖
  ``XProducer.dep(config, default=...)``。

具名实例缓存归属 AssemblyContext；独立装配互不复用，builder 中的隐式缓存操作只作用当前上下文。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from threading import RLock
from typing import Any

from jiuwen_memory.common.errors import ValidationError

Builder = Callable[..., Any]
_BUILD_OBSERVERS: ContextVar[tuple[Callable[[object], None], ...]] = ContextVar(
    "factory_build_observers", default=()
)


@dataclass
class InstanceCache:
    """单次装配拥有的具名实例及生命周期；不包含插件注册表。"""

    instances: dict[type, dict[str, Any]] = dataclass_field(default_factory=dict)
    epoch: object = dataclass_field(default_factory=object)
    lifecycle: object | None = None
    lock: Any = dataclass_field(default_factory=RLock, repr=False)


_DEFAULT_CACHE = InstanceCache()
_CURRENT_CACHE: ContextVar[InstanceCache | None] = ContextVar(
    "factory_instance_cache", default=None
)


def _instance_cache(ctx: Any = None) -> InstanceCache:
    if ctx is not None:
        return ctx.instance_cache
    return _CURRENT_CACHE.get() or _DEFAULT_CACHE


class Factory:
    """注册式工厂：子类注册 builder，装配上下文持有具名实例缓存。"""

    # 该 Producer 的全局唯一顶层命名空间名（如 "kv_store"）；空=未迁移到新模型
    TOP_NAME: str = ""
    _registry: dict[str, Builder]
    _config_inspectors: dict[str, dict[str, Builder]]
    _by_top_name: dict[str, type] = {}  # TOP_NAME -> producer 子类（供 Config 解析期校验顶层段）

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._registry = {}  # 每个 producer 子类一张独立注册表
        cls._config_inspectors = {}
        top = cls.__dict__.get("TOP_NAME", "")  # 仅认本类显式声明的（不继承父类）
        if top:
            prev = Factory._by_top_name.get(top)
            if prev is not None and prev is not cls:
                raise ValidationError(
                    f"TOP_NAME {top!r} 冲突：{prev.__name__} 与 {cls.__name__} 重名"
                )
            Factory._by_top_name[top] = cls

    @staticmethod
    def cfg_get(config: Any, name: str, default: Any = None) -> Any:
        """从本组件配置（ComponentSpec/参数视图）读一个参数：优先 ``.get(name)``，
        回退属性访问，缺失给 ``default``——供 builder 统一取自己 ``params`` 里的连接参数。
        """
        getter = getattr(config, "get", None)
        if callable(getter):
            return getter(name, default)
        return getattr(config, name, default)

    @staticmethod
    def require_param(config: Any, name: str, *, backend: str) -> Any:
        """读**必填**参数；未配置（None/空）时在 *build 阶段*抛 :class:`ValidationError`。

        依赖三方库/外部服务的后端（redis/elasticsearch/milvus …）缺了 url/hosts/uri 这类
        必填项时，宁可装配时就报错，也好过惰性连接阶段才暴露。
        这样能让配置错误尽早、清晰地暴露。
        """
        value = Factory.cfg_get(config, name)
        if value is None or value == "":
            raise ValidationError(
                f"{backend} 后端缺少必填参数 params.{name!r}（请在该组件 params 下配置）"
            )
        return value

    @classmethod
    def known_top_names(cls) -> set:
        """已注册的 Producer 顶层命名空间名集合（供 Config 解析期校验）。"""
        return set(Factory._by_top_name)

    @classmethod
    def register(
        cls, target: str, *, config_inspectors: dict[str, Builder] | None = None
    ) -> Callable[[Builder], Builder]:
        """装饰器：把一个 builder 以实现名 ``target`` 注册进本 producer。"""

        def deco(builder: Builder) -> Builder:
            cls._registry[target] = builder
            # 替换同名插件时不得继承旧实现的能力声明。
            cls._config_inspectors[target] = dict(config_inspectors or {})
            return builder

        return deco

    @classmethod
    @contextmanager
    def registration_scope(cls) -> Iterator[None]:
        """Temporarily register plugins; restore builders and inspectors even on failure.

        This is an assembly/test isolation boundary, not a concurrent registry transaction.
        Instance caches have their existing reset_all lifecycle.
        """
        builders = dict(cls._registry)
        inspectors = {target: dict(checks) for target, checks in cls._config_inspectors.items()}
        try:
            yield
        finally:
            cls._registry = builders
            cls._config_inspectors = inspectors

    @classmethod
    def for_namespace(cls, namespace: str) -> type[Factory]:
        """从公开命名空间查 Producer，不构建插件实例。"""
        producer = Factory._by_top_name.get(namespace)
        if producer is None:
            raise ValidationError(f"unknown producer namespace: {namespace!r}")
        return producer

    @classmethod
    def inspect_config(cls, target: str, inspector: str, config: Any) -> Any:
        """执行插件注册的无副作用配置能力检查；未声明能力时明确拒绝。"""
        inspect = cls._config_inspectors.get(target, {}).get(inspector)
        if inspect is None:
            raise ValidationError(
                f"{cls.TOP_NAME}.{config.name}: {inspector} contract is unknown for {target!r}"
            )
        return inspect(config)

    # ====================================================================== #
    # 装配模型（两级命名空间）：接口 1 匿名 build / 接口 2 具名 build_named /
    # builder 内取依赖 dep。详见 :mod:`config.context` 与 ``api.build_kernel``。
    # ====================================================================== #

    @classmethod
    def build(cls, target: str, params: Any, ctx: Any, *, name: str = "") -> Any:
        """接口 1（匿名）：按 ``target`` 查注册的 ``_build`` 新建实例，**不入缓存**。

        ``params`` 是字面参数；``ctx`` 是 :class:`~config.context.AssemblyContext`（供该
        实例的 ``_build`` 内再取嵌套依赖）。
        真正 new 实例的逻辑在各 impl 注册的 ``_build`` 里。
        """
        from jiuwen_memory.config.context import ComponentConfig

        builder = cls._registry.get(target)
        if builder is None:
            raise ValidationError(
                f"{cls.__name__}: 未注册的实现 {target!r}（已注册：{cls.known()}）"
            )
        config = ComponentConfig(params=dict(params or {}), ctx=ctx, target=target, name=name)
        token = _CURRENT_CACHE.set(_instance_cache(ctx))
        try:
            instance = builder(config)
            for observer in _BUILD_OBSERVERS.get():
                observer(instance)
            return instance
        finally:
            _CURRENT_CACHE.reset(token)

    @classmethod
    def build_named(cls, name: str, ctx: Any) -> Any:
        """接口 2（具名/共享）：按 ``name`` 取/建该 Producer 命名空间下的具名实例。

        命中缓存即返回共享实例；否则经 ``ctx.lookup(cls.TOP_NAME, name)`` 取配置、调
        :meth:`build` 新建。``new_instance`` 为假则存入缓存供共享，为真则不存（每次新建）。
        命名空间由 ``cls.TOP_NAME`` 决定——故只需传具名实例名。
        """
        cache = _instance_cache(ctx)
        with cache.lock:
            instances = cache.instances.setdefault(cls, {})
            if name in instances:
                return instances[name]
            spec = ctx.lookup(cls.TOP_NAME, name)
            instance = cls.build(spec.target, spec.params, ctx, name=name)
            if not spec.new_instance:
                instances[name] = instance
            return instance

    @classmethod
    def bind_named(cls, name: str, ctx: Any) -> Callable[[], Any]:
        """延迟解析指定上下文的具名依赖；绑定时不查配置、不创建对象。

        上下文持有共享缓存与可重入锁，独立装配及全局 reset 不影响本句柄。
        保留绑定时的创建观察者，嵌套依赖和 ConfigSource 均由 builder 继承该上下文。
        """
        observers = _BUILD_OBSERVERS.get()

        def resolve() -> Any:
            observer_token = _BUILD_OBSERVERS.set(observers)
            try:
                return cls.build_named(name, ctx)
            finally:
                _BUILD_OBSERVERS.reset(observer_token)

        return resolve

    @classmethod
    def dep(cls, config: Any, param_name: str | None = None, default: str | None = None) -> Any:
        """builder 内取一个依赖：按字段值分派「引用 / 内联 / 缺省」到对应接口。

        - ``param_name`` 缺省取 ``cls.TOP_NAME``（字段名与本 Producer 顶层名一致时不必传）；
          不一致时显式传（如 ``KvProducer.dep(config, "data_store")``）。
        - 字段值为 **字符串** → 引用：:meth:`build_named`（共享）。
        - 字段值为 **映射** → 内联匿名实例：:meth:`build`（不共享）。
        - 字段 **缺省** → 用 ``default`` 实现匿名新建（不共享）；无 ``default`` 则报错。
        """
        field = param_name or cls.TOP_NAME
        ctx = config.ctx
        value = config.params.get(field)
        if value is None:
            if default is None:
                raise ValidationError(f"{cls.__name__}.dep: 依赖 {field!r} 未配置且无默认实现")
            return cls.build(default, {}, ctx)
        if isinstance(value, str):
            return cls.build_named(value, ctx)
        if isinstance(value, dict):
            target = value.get("target")
            if not target:
                raise ValidationError(f"{cls.__name__}.dep: 内联依赖 {field!r} 缺少 'target'")
            return cls.build(target, value.get("params", {}), ctx, name=value.get("name", ""))
        raise ValidationError(
            f"{cls.__name__}.dep: 依赖 {field!r} 应是引用名(str)或内联配置(dict)，"
            f"得到 {type(value).__name__}"
        )

    @classmethod
    def put(cls, name: str, instance: Any, *, ctx: Any = None) -> None:
        """把一个外部实例以 ``name`` 预置进缓存（如显式注入的真源后端）。"""
        cache = _instance_cache(ctx)
        with cache.lock:
            cache.instances.setdefault(cls, {})[name] = instance

    @classmethod
    def get_cached(cls, name: str, *, ctx: Any = None) -> Any:
        """只查询指定上下文（builder 内缺省当前上下文），不隐式触发新装配。"""
        cache = _instance_cache(ctx)
        with cache.lock:
            return cache.instances.get(cls, {}).get(name)

    @classmethod
    def reset_instances(cls, *, ctx: Any = None) -> None:
        """清空本 producer 的具名实例缓存。"""
        cache = _instance_cache(ctx)
        with cache.lock:
            cache.instances.pop(cls, None)

    @classmethod
    def reset_all(cls, *, ctx: Any = None) -> None:
        """清空指定装配缓存；无上下文时只清理当前 builder 或独立的旧式默认缓存。"""
        cache = _instance_cache(ctx)
        with cache.lock:
            cache.epoch = object()
            cache.lifecycle = None
            cache.instances.clear()

    @staticmethod
    def cache_epoch(*, ctx: Any = None) -> object:
        """返回当前缓存装配纪元的 opaque 标识，不暴露实例私有状态。"""
        return _instance_cache(ctx).epoch

    @classmethod
    def reset_if_current_epoch(cls, epoch: object, *, ctx: Any = None) -> None:
        """只失效仍属于本次装配的缓存，旧所有者不得清空较新装配。"""
        cache = _instance_cache(ctx)
        with cache.lock:
            if cache.epoch is epoch:
                cls.reset_all(ctx=ctx)

    @staticmethod
    def bind_cache_lifecycle(
        owner: object, share: Callable[[object, object], None], *, ctx: Any = None
    ) -> None:
        """装配根显式绑定本纪元所有者；同一具名图的新增视图按给定协议共享。"""
        cache = _instance_cache(ctx)
        with cache.lock:
            if cache.lifecycle is None:
                cache.lifecycle = owner
            else:
                share(cache.lifecycle, owner)

    @classmethod
    def discard_created(cls, instances: tuple[object, ...], *, ctx: Any = None) -> None:
        """失败装配仅移除新建对象的缓存项；既有借用实例保持原样。"""
        identities = {id(instance) for instance in instances}
        cache = _instance_cache(ctx)
        with cache.lock:
            for producer, cached in cache.instances.items():
                retained = {}
                for name, instance in cached.items():
                    if id(instance) not in identities:
                        retained[name] = instance
                cache.instances[producer] = retained

    @staticmethod
    @contextmanager
    def observe_builds(observer: Callable[[object], None]) -> Iterator[None]:
        """装配根观察本作用域新建对象（含匿名依赖），不报告缓存命中/put 借用。

        嵌套作用域同时通知外层观察者；结束或异常时恢复原观察集合。
        ContextVar 仅隔离同步装配记录，不承载认证身份或业务请求上下文。
        """
        token = _BUILD_OBSERVERS.set((*_BUILD_OBSERVERS.get(), observer))
        try:
            yield
        finally:
            _BUILD_OBSERVERS.reset(token)

    @classmethod
    def known(cls) -> list[str]:
        """已注册的实现名（排序）。"""
        return sorted(cls._registry)
