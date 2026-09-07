# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""内核组装：从顶层组件出发，经各组件 Producer 装配成 MemoryAPI。

设计要点：

- **取依赖统一走 `dep`**：``XProducer.dep(config, default=...)`` 按配置值分派——引用名(str)→
  ``build_named`` 共享、内联(dict)→``build`` 匿名、缺省(None)→按 ``default`` 匿名新建。字段名
  默认取 ``XProducer.TOP_NAME``（engine / permission / scheduler / policy / governor / audit /
  kv_store 各自一致），故顶层只需给缺省实现名。
- **共享靠显式具名**：要让 index_builder 与 vector_recaller 取到同一个 VectorStore，在配置里
  给该 store 起名、两处引用同名即可（见 :mod:`config.context`）。
- **缺省**：配置未给某项时第二参即缺省实现；跨切面参数缺省写在各 ``_build`` 的
  ``config.get("vector_enabled", True)`` 读取处，配置的 ``globals`` 可覆盖之。
- **真源 kv 注入**：``kv`` 入参经 ``KvProducer.put`` 预置进缓存，覆盖配置选择并被各处共享。

- **默认装配**：无 config 时用内置默认上下文（:mod:`config.defaults`）——离线进程内栈，用
  显式具名 + 引用复刻共享拓扑；用户 config **合并覆盖**到其上（只写要改动的部分）。
- **真源 kv 注入**：``kv`` 入参经 ``KvProducer.put`` 预置进缓存，覆盖配置选择并被各处共享。

注册靠 import 各实现模块触发，组装前由各层 bootstrap 统一拉起（句柄住在接口层）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.audit.base import AuditProducer
from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import setup_logging
from jiuwen_memory.common.security.audit_integrity.base import (
    DEFAULT_AUDIT_VERIFY_MAX_SAMPLES,
    DEFAULT_AUDIT_VERIFY_PAGE_SIZE,
    AuditVerificationLimits,
)
from jiuwen_memory.config import Config
from jiuwen_memory.config.config_source import ConfigSource, ConfigSourceProducer
from jiuwen_memory.config.config_source_impl import register_config_sources
from jiuwen_memory.config.context import AssemblyContext, ComponentConfig
from jiuwen_memory.config.defaults import KV_DEFAULT_NAME, ROOT_PARAMS, default_context
from jiuwen_memory.construction.bootstrap import register_constructors
from jiuwen_memory.construction.router import optional_router
from jiuwen_memory.control.bootstrap import register_controllers
from jiuwen_memory.control.engine import EngineProducer
from jiuwen_memory.control.governance import GovernorProducer
from jiuwen_memory.control.ingest_job import IngestJobController, IngestJobProducer
from jiuwen_memory.control.membership import MembershipProducer
from jiuwen_memory.control.permission import PermissionProducer
from jiuwen_memory.control.policy import PolicyProducer
from jiuwen_memory.control.scheduler import SchedulerProducer
from jiuwen_memory.control.space import SpaceManager, SpaceProducer
from jiuwen_memory.ingest.bootstrap import register_ingestors
from jiuwen_memory.retrieval.bootstrap import register_operators
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.store_manager import (
    StoreManager,
    StoreManagerProducer,
    resolve_name,
)

from ..memory_api import MemoryAPI
from .local_memory_api import LocalMemoryAPI

_SCHEMA_TARGETS = frozenset(
    {
        ("extractor", "entity_schema"),
        ("evolver", "schema_orchestrating"),
    }
)


def _configured_schema_targets(ctx: AssemblyContext) -> list[str]:
    selected: list[str] = []
    for namespace, target in _SCHEMA_TARGETS:
        for name, spec in ctx.namespaces.get(namespace, {}).items():
            if spec.target == target:
                selected.append(f"{namespace}.{name}={target}")
    return sorted(selected)


@dataclass
class _Kernel:
    """装配模块内部工作对象：同时持有 API 与原始端口。

    不是库的公共能力。Access / SDK / HTTP / MCP 只能拿到 :class:`MemoryAPI`
    或 :class:`MemoryRuntime`。

    Attributes:
        api: 形态无关的 MemoryAPI 入口
        kv: 真源 KV（全局 StoreManager 的 KV 端口，与 ``kv_store.default`` 具名实例
            同源——外部注入的 kv 经 ``KvProducer.put`` 预置缓存后被各处共享；
            F04 §5.4 默认 raw，opt-in encrypted target 才包装）
        storage: 上层统一使用的 StoreManager（默认 CompositeStoreManager；数据面
            领域操作经 ``storage.domain_store()``；配置段名 ``store_manager:``）
        space: SpaceManager（若装配）
        config_source: 运行时晚绑定配置来源（默认 YamlDefaultsConfigSource）
    """

    api: LocalMemoryAPI
    kv: KVStore
    storage: StoreManager
    ingest_jobs: IngestJobController
    space: SpaceManager | None = None
    config_source: ConfigSource | None = None


@runtime_checkable
class MemoryRuntime(Protocol):
    """Access composition root 使用的运行时句柄：只有 API 与关闭语义。"""

    @property
    def api(self) -> MemoryAPI:
        ...

    def close(self, *, wait: bool = True) -> None:
        ...


@dataclass
class _MemoryRuntime:
    api: MemoryAPI
    _ingest_jobs: IngestJobController

    def close(self, *, wait: bool = True) -> None:
        self._ingest_jobs.close(wait=wait)


def _coerce_config(config: Config | Mapping[str, Any] | None) -> Config | None:
    """Access 只传 dict / None；内核测试仍可传 ``Config``。"""
    if config is None or isinstance(config, Config):
        return config
    if isinstance(config, Mapping):
        return Config.from_dict(config)
    raise TypeError(
        "assemble config must be Config, mapping, or None, "
        f"got {type(config).__name__}"
    )


def _register_all() -> None:
    """组装前按层触发自注册（句柄在接口、注册靠 import 实现；各 bootstrap 幂等）。"""
    register_plugins()       # common 共享插件
    register_backends()      # storage
    register_operators()     # retrieval
    register_ingestors()     # ingest
    register_constructors()  # construction
    register_controllers()   # control
    register_config_sources()  # ConfigSource：yaml_defaults / dict / overlay


def _audit_verify_config_int(root: ComponentConfig, key: str, default: int) -> int:
    """读取 ``globals`` 中的审计验证整数；不把字符串静默强制转换为数字。"""
    raw = root.get(key, default)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    raise ValidationError(f"globals.{key} must be an integer, got {raw!r}")


def _audit_verify_limits(root: ComponentConfig) -> AuditVerificationLimits:
    """从可信服务端 globals 构造 PEP 上限，屏蔽值对象的裸类型/范围异常。"""
    max_page_size = _audit_verify_config_int(
        root,
        "audit_verify_max_page_size",
        DEFAULT_AUDIT_VERIFY_PAGE_SIZE,
    )
    max_samples = _audit_verify_config_int(
        root,
        "audit_verify_max_samples",
        DEFAULT_AUDIT_VERIFY_MAX_SAMPLES,
    )
    try:
        return AuditVerificationLimits(
            max_page_size=max_page_size,
            max_samples=max_samples,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"invalid audit verification limits: {exc}") from None


def _build_kernel(
    policies: dict[str, str] | None = None,
    kv: KVStore | None = None,
    config: Config | Mapping[str, Any] | None = None,
) -> _Kernel:
    """把配置装配成内部内核（api + 真源）。各组件经引用自取依赖、缺省随调用点给出。

    - ``config``：用户配置（两级命名空间 ``Config`` 或等价 dict），**合并覆盖**到内置默认
      （:mod:`config.defaults`）之上；``None`` 时纯用内置默认（离线进程内栈）。
      Access 只传 dict，不 import ``jiuwen_memory.config``。
    - ``policies``：便捷覆盖运行时策略（折进 ``globals.policies``）。
    - ``kv``：显式注入真源后端，覆盖配置的 kv_store 选择（如传 ``SQLiteKVStore`` 即落盘）。
    """
    config = _coerce_config(config)
    if config is not None and not config.is_empty():
        requested = config.context()
        if "audit_integrity" in requested.namespaces:
            raise ValidationError(
                "audit_integrity is interface-only: no implementation target is registered"
            )
    _register_all()
    Factory.reset_all()  # 每次组装前清空具名实例缓存以隔离多次装配

    ctx = default_context()
    if config is not None and not config.is_empty():
        ctx = ctx.merged(config.context(known_top_names=Factory.known_top_names()))
    if policies is not None:
        ctx.globals["policies"] = dict(policies)
    schema_enabled = as_bool(ctx.globals.get("schema_enabled"), default=False)
    configured_schema_targets = _configured_schema_targets(ctx)
    if configured_schema_targets and not schema_enabled:
        configured = ", ".join(configured_schema_targets)
        raise ValidationError(
            "Schema targets require globals.schema_enabled=true; configured: " + configured
        )
    if schema_enabled:
        # Schema 实现不在默认导入路径里。默认是关闭。配置允许我解析任何组件依赖之前就注册
        from jiuwen_memory.construction.schema_bootstrap import register_schema_constructors

        register_schema_constructors()
    if kv is not None:
        KvProducer.put(KV_DEFAULT_NAME, kv)  # 外部注入的真源覆盖配置选择，并被各处共享

    # 根组件（LocalMemoryAPI）经 ROOT_PARAMS 引用各命名空间下的 default 实例。
    root = ComponentConfig(params=dict(ROOT_PARAMS), ctx=ctx, target="local", name="memory_api")
    setup_logging(root)  # 初始化 agent-memory 根 logger（按 globals 的 log_* 配置；幂等）

    # ConfigSource 须先于 engine/evolver 装配，供 PromptRegistry / 插件晚绑定共享。
    config_source = ConfigSourceProducer.dep(root, default="yaml_defaults")
    if not isinstance(config_source, ConfigSource):
        raise ValidationError(
            f"config_source 装配结果不是 ConfigSource: {type(config_source).__name__}"
        )
    ConfigSourceProducer.put("default", config_source)

    storage = StoreManagerProducer.resolve(root)
    if not isinstance(storage, StoreManager):
        raise ValidationError(
            f"storage namespace assembled a non-StoreManager value: {type(storage).__name__}"
        )
    # Kernel.kv 统一从 StoreManager 端口取：composite 的 KV 端口与 kv_store.default
    # 具名实例同源（外部注入的 kv 经 KvProducer.put 预置缓存，composite 内 dep 命中
    # 同一实例），授权代理默认 AllowAll、行为与裸 KVStore 一致。ROOT_PARAMS 的
    # kv_store 键即端口名（用户可指名其他 kv 端口）。
    kv_store = storage.kv(resolve_name(root, "kv_store"))

    ingest_jobs = IngestJobProducer.dep(root, default="in_process")
    if not isinstance(ingest_jobs, IngestJobController):
        raise ValidationError(
            "ingest_job namespace assembled a non-IngestJobController value: "
            f"{type(ingest_jobs).__name__}"
        )
    space = SpaceProducer.dep(root, default="kv")

    api = LocalMemoryAPI(
        engine=EngineProducer.dep(root, default="in_memory"),
        permission=PermissionProducer.dep(root, default="sqlite"),
        scheduler=SchedulerProducer.dep(root, default="in_process"),
        policy=PolicyProducer.dep(root, default="dict"),
        governor=GovernorProducer.dep(root, default="in_memory"),
        audit_logger=AuditProducer.dep(root, default="sqlite"),
        space=space,
        ingest_jobs=ingest_jobs,
        # 单次扫描量/返回样本量是可信服务端配置，不从请求 payload 读取。即使完整性
        # provider 尚未装配，也先固定同一组装配键，后续实装无需再改 PEP 构造签名。
        audit_verify_limits=_audit_verify_limits(root),
        # 空间授权事实的读取算子，与 space / policy / governor 同为 ROOT_PARAMS 里声明的
        # 根组件，因此一律装配，不按判定实现的能力声明按需建。判定实现声明不需要空间事实时
        # 鉴权点确实不调用它，但跨空间检索的候选空间反查不经判定实现，按能力声明跳过装配
        # 会让未开空间治理的部署静默拿到空候选集。实例本身是共享 SpaceManager 加一个无状态
        # 索引包装，不持有连接或线程。
        membership=MembershipProducer.dep(root, default="kv"),
        # 归属判定算子。未声明 router 命名空间即为 None：判定表为空、写入侧 scope 必填、
        # 判定路径不可达，全链路行为与未启用该特性一致。
        #
        # 是否与构建层共用一份判定表取决于配置写法，不是无条件成立：经 router.default
        # 具名引用时 Factory 缓存具名实例，本层与两个 Evolver 取到同一个对象；而 Evolver
        # 的组件配置里内联 router 参数时，optional_router 走 dep 的内联分支建匿名实例，
        # 两侧判定表可以不同。此时写入边界拒绝的键集合按本层的表算、实际落点按构建层的
        # 表算，两者错位。判定表不另设解析路径的理由见 F07「归属判定算子」，那一条约束的
        # 是同一实例内不出现两份表，跨实例的一致由本处的配置写法决定。
        router=optional_router(root),
    )
    _reject_routing_without_space_authorization(api)
    return _Kernel(
        api=api,
        kv=kv_store,
        storage=storage,
        ingest_jobs=ingest_jobs,
        space=space,
        config_source=config_source,
    )


def _reject_routing_without_space_authorization(api: LocalMemoryAPI) -> None:
    """拒绝「判定表已配置而判定实现不读空间事实」的组合（F07「两个开关」）。

    两个开关互不相关、可各自开启，四种组合里只有这一种是配置错误：内容按坐标分流进协作
    空间，而协作空间没有任何权限边界，等于把内容写进一个组织内任意主体可读的位置。方向
    为放行，且从调用侧看不出异常——写入成功、检索也拿得到，只是拿得到的人多了。

    另一个方向（只开空间治理不开判定表）是合法部署：空间之间有权限边界，``scope`` 仍必填。
    """
    # 同包内的装配期一致性校验，读的是本包实现自身的内部判据，不经对外契约。
    if not api._routing_enabled() or api._needs_space_facts():  # pylint: disable=protected-access
        return
    raise ValidationError(
        "配置了 router 判定表但未开启空间治理。改法二选一："
        "把 permission.default.target 配成 space_aware，或删掉 router 命名空间。"
        "原因：判定表把写入分流进协作空间，而当前的判定实现不读空间事实，"
        "这些协作空间没有任何权限边界——写入成功、检索也拿得到，只是拿得到的人多了。"
    )


def assemble(
    policies: dict[str, str] | None = None,
    kv: KVStore | None = None,
    config: Config | Mapping[str, Any] | None = None,
) -> MemoryAPI:
    """装配出一个可用的 ``MemoryAPI``。普通调用方只应使用本函数。"""
    return _build_kernel(policies, kv, config).api


def assemble_runtime(
    policies: dict[str, str] | None = None,
    kv: KVStore | None = None,
    config: Config | Mapping[str, Any] | None = None,
) -> MemoryRuntime:
    """装配 Access 运行时：``api`` + ``close``，不暴露存储或任务控制器端口。"""
    kernel = _build_kernel(policies, kv, config)
    return _MemoryRuntime(api=kernel.api, _ingest_jobs=kernel.ingest_jobs)
