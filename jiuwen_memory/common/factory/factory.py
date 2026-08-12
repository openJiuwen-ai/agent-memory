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

from typing import Any, Callable, Dict, List

from jiuwen_memory.common.errors import ValidationError

Builder = Callable[..., Any]


class Factory:
    """注册式工厂：子类各自持有 ``target → builder`` 表与 ``name → 实例`` 缓存。"""

    # 该 Producer 的全局唯一顶层命名空间名（如 "kv_store"）；空=未迁移到新模型
    TOP_NAME: str = ""
    _registry: Dict[str, Builder]
    _instances: Dict[str, Any]
    _subclasses: List[type] = []  # 所有 producer 子类（供 reset_all 统一清缓存）
    _by_top_name: Dict[str, type] = {}  # TOP_NAME -> producer 子类（供 Config 解析期校验顶层段）

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
        return builder(config)

    @classmethod
    def build_named(cls, name: str, ctx: Any) -> Any:
        """接口 2（具名/共享）：按 ``name`` 取/建该 Producer 命名空间下的具名实例。

        命中缓存即返回共享实例；否则经 ``ctx.lookup(cls.TOP_NAME, name)`` 取配置、调
        :meth:`build` 新建。``new_instance`` 为假则存入缓存供共享，为真则不存（每次新建）。
        命名空间由 ``cls.TOP_NAME`` 决定——故只需传具名实例名。
        """
        if name in cls._instances:
            return cls._instances[name]
        spec = ctx.lookup(cls.TOP_NAME, name)
        instance = cls.build(spec.target, spec.params, ctx, name=name)
        if not spec.new_instance:
            cls._instances[name] = instance
        return instance

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
    def put(cls, name: str, instance: Any) -> None:
        """把一个外部实例以 ``name`` 预置进缓存（如显式注入的真源后端）。"""
        cls._instances[name] = instance

    @classmethod
    def reset_instances(cls) -> None:
        """清空本 producer 的具名实例缓存。"""
        cls._instances = {}

    @classmethod
    def reset_all(cls) -> None:
        """清空所有 producer 的具名实例缓存。"""
        for sub in Factory._subclasses:
            sub.reset_instances()

    @classmethod
    def known(cls) -> List[str]:
        """已注册的实现名（排序）。"""
        return sorted(cls._registry)
