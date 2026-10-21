# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Factory — 注册式工厂基类（开闭：分发封闭、实现可插拔）+ 具名实例共享。

每个 producer 继承本类，声明全局唯一的 ``TOP_NAME``（配置里的顶层命名空间），
并得到**独立的**
``target → builder`` 注册表与 ``name → 实例`` 缓存。具体实现用 ``@SomeProducer.register("实现名")``
把一个 ``_build(config) -> 实例`` 注册进来（接口 1 匿名创建的实现体）。在此之上：

- :meth:`build`（接口 1，匿名）：按 ``target`` 新建、不入缓存。
- :meth:`build_named`（接口 2，具名/共享）：按具名实例名经 ``ctx.lookup(TOP_NAME, name)`` 取配置、
  新建并按 ``new_instance`` 决定是否入缓存共享。
- :meth:`dep`：builder 内取一个依赖，按字段值
  「引用(str)→共享 / 内联(dict)→匿名 / 缺省→匿名」分派。
- ``config``：传给 ``_build`` 的 :class:`~config.context.ComponentConfig`——读参数
  ``config.get(...)``（本实例 params 回退 globals）、取依赖
  ``XProducer.dep(config, default=...)``。

装配编排（``api.build_kernel``）在每次组装前 :meth:`reset_all` 清空实例缓存以隔离多次装配。
"""

from __future__ import annotations

from _thread import RLock
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.errors import ValidationError

Builder = Callable[..., Any]
_BUILD_OBSERVERS: ContextVar[tuple[Callable[[object], None], ...]] = ContextVar(
    "factory_build_observers", default=()
)


@dataclass
class _InstanceBinding:
    """持有装配时的缓存原对象；后续 reset 只替换全局引用。"""

    caches: dict[type, dict[str, Any]]
    lock: RLock


_bound_instances: ContextVar[_InstanceBinding | None] = ContextVar(
    "factory_bound_instances", default=None,
)


class Factory:
    """注册式工厂：子类各自持有 ``target → builder`` 表与 ``name → 实例`` 缓存。"""

    # 该 Producer 的全局唯一顶层命名空间名（如 "kv_store"）；空=未迁移到新模型
    TOP_NAME: str = ""
    _registry: dict[str, Builder]
    _instances: dict[str, Any]
    _subclasses: list[type] = []  # 所有 producer 子类（供 reset_all 统一清缓存）
    _by_top_name: dict[str, type] = {}  # TOP_NAME -> producer 子类（供 Config 解析期校验顶层段）
    _instance_lock = RLock()
    _cache_epoch: object = object()
    _cache_lifecycle: object | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._registry = {}  # 每个 producer 子类一张独立注册表
        cls._instances = {}  # 每个 producer 子类一份具名实例缓存
        Factory._subclasses.append(cls)
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
    def register(cls, target: str) -> Callable[[Builder], Builder]:
        """装饰器：把一个 builder 以实现名 ``target`` 注册进本 producer。"""

        def deco(builder: Builder) -> Builder:
            cls._registry[target] = builder
            return builder

        return deco

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
        instance = builder(config)
        for observer in _BUILD_OBSERVERS.get():
            observer(instance)
        return instance

    @classmethod
    def build_named(cls, name: str, ctx: Any) -> Any:
        """接口 2（具名/共享）：按 ``name`` 取/建该 Producer 命名空间下的具名实例。

        命中缓存即返回共享实例；否则经 ``ctx.lookup(cls.TOP_NAME, name)`` 取配置、调
        :meth:`build` 新建。``new_instance`` 为假则存入缓存供共享，为真则不存（每次新建）。
        命名空间由 ``cls.TOP_NAME`` 决定——故只需传具名实例名。
        """
        with cls._cache_lock():
            instances = cls._instance_cache()
            if name in instances:
                return instances[name]
            spec = ctx.lookup(cls.TOP_NAME, name)
            instance = cls.build(spec.target, spec.params, ctx, name=name)
            if not spec.new_instance:
                instances[name] = instance
            return instance

    @classmethod
    def _instance_cache(cls) -> dict[str, Any]:
        """具名缓存的统一入口，兼容装配内与已绑定的延迟解析。"""
        bound = _bound_instances.get()
        if bound is None:
            return cls._instances
        # 晚注册的 Producer 在旧装配内也不能借用新装配的缓存。
        return bound.caches.setdefault(cls, {})

    @classmethod
    def _cache_lock(cls) -> RLock:
        bound = _bound_instances.get()
        return bound.lock if bound is not None else Factory._instance_lock

    @classmethod
    def bind_named(cls, name: str, ctx: Any) -> Callable[[], Any]:
        """绑定本轮装配的具名依赖，首次调用前不查配置、不创建组件。

        保留本轮各 Producer 缓存的引用，因此后续 ``reset_all`` 不会改变本句柄的
        嵌套依赖及 ConfigSource 来源；同轮句柄共用锁与缓存，首次并发解析仍共享实例。
        ``new_instance`` 语义与 ``build_named`` 一致，失败不缓存且恢复调用方上下文。
        """
        bound = _bound_instances.get()
        if bound is None:
            with Factory._instance_lock:
                # Factory 统一管理自己注册的子类缓存，须保留原字典引用。
                # 与 discard_created 相同，这是工厂内部管理，不是访问外部插件私有状态。
                # pylint: disable=protected-access
                bound = _InstanceBinding(
                    {producer: producer._instances for producer in Factory._subclasses},
                    Factory._instance_lock,
                )
                # pylint: enable=protected-access

        observers = _BUILD_OBSERVERS.get()

        def resolve() -> Any:
            token = _bound_instances.set(bound)
            observer_token = _BUILD_OBSERVERS.set(observers)
            try:
                return cls.build_named(name, ctx)
            finally:
                _BUILD_OBSERVERS.reset(observer_token)
                _bound_instances.reset(token)

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
    def put(cls, name: str, instance: Any) -> None:
        """把一个外部实例以 ``name`` 预置进缓存（如显式注入的真源后端）。"""
        cls._instance_cache()[name] = instance

    @classmethod
    def reset_instances(cls) -> None:
        """清空本 producer 的具名实例缓存。"""
        cls._instances = {}

    @classmethod
    def reset_all(cls) -> None:
        """清空所有 producer 的具名实例缓存。"""
        with Factory._instance_lock:
            Factory._cache_epoch = object()
            Factory._cache_lifecycle = None
            for sub in Factory._subclasses:
                sub.reset_instances()
            Factory._instance_lock = RLock()

    @staticmethod
    def cache_epoch() -> object:
        """返回当前缓存装配纪元的 opaque 标识，不暴露实例私有状态。"""
        return Factory._cache_epoch

    @classmethod
    def reset_if_current_epoch(cls, epoch: object) -> None:
        """只失效仍属于本次装配的缓存，旧所有者不得清空较新装配。"""
        if Factory._cache_epoch is epoch:
            cls.reset_all()

    @staticmethod
    def bind_cache_lifecycle(owner: object, share: Callable[[object, object], None]) -> None:
        """装配根显式绑定本纪元所有者；同一具名图的新增视图按给定协议共享。"""
        if Factory._cache_lifecycle is None:
            Factory._cache_lifecycle = owner
        else:
            share(Factory._cache_lifecycle, owner)

    @classmethod
    def discard_created(cls, instances: tuple[object, ...]) -> None:
        """失败装配仅移除新建对象的缓存项；既有借用实例保持原样。"""
        # 本工厂统一管理各 Producer 的缓存；不是探测第三方插件私有能力。
        # pylint: disable=protected-access
        identities = {id(instance) for instance in instances}
        for producer in Factory._subclasses:
            retained = {}
            for name, instance in producer._instances.items():
                if id(instance) not in identities:
                    retained[name] = instance
            producer._instances = retained

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
