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

from dataclasses import dataclass

from jiuwen_memory.common.audit.base import AuditProducer
from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import setup_logging
from jiuwen_memory.config import Config
from jiuwen_memory.config.config_source import ConfigSource, ConfigSourceProducer
from jiuwen_memory.config.config_source_impl import register_config_sources
from jiuwen_memory.config.context import ComponentConfig
from jiuwen_memory.config.defaults import KV_DEFAULT_NAME, ROOT_PARAMS, default_context
from jiuwen_memory.construction.bootstrap import register_constructors
from jiuwen_memory.control.bootstrap import register_controllers
from jiuwen_memory.control.engine import EngineProducer
from jiuwen_memory.control.governance import GovernorProducer
from jiuwen_memory.control.ingest_job import IngestJobController, IngestJobProducer
from jiuwen_memory.control.permission import PermissionProducer
from jiuwen_memory.control.policy import PolicyProducer
from jiuwen_memory.control.scheduler import SchedulerProducer
from jiuwen_memory.control.space import SpaceManager, SpaceProducer
from jiuwen_memory.ingest.bootstrap import register_ingestors
from jiuwen_memory.retrieval.bootstrap import register_operators
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.storage import Storage, StorageProducer

from .local_memory_api import LocalMemoryAPI


@dataclass
class Kernel:
    """一次装配的产物：对外 API、统一 Storage、兼容 KV 句柄与配置来源。

    Attributes:
        api: 形态无关的 MemoryAPI 入口
        kv: 真源 KV（按 ``kv_store.default`` 装配；F04 §5.4 默认 raw，
            opt-in encrypted target 才包装）
        storage: 上层统一使用的 Storage（默认 CompositeStorage）
        space: SpaceManager（若装配）
        config_source: 运行时晚绑定配置来源（默认 YamlDefaultsConfigSource）
    """

    api: LocalMemoryAPI
    kv: KVStore
    storage: Storage
    ingest_jobs: IngestJobController
    space: SpaceManager | None = None
    config_source: ConfigSource | None = None


def _register_all() -> None:
    """组装前按层触发自注册（句柄在接口、注册靠 import 实现；各 bootstrap 幂等）。"""
    register_plugins()       # common 共享插件
    register_backends()      # storage
    register_operators()     # retrieval
    register_ingestors()     # ingest
    register_constructors()  # construction
    register_controllers()   # control
    register_config_sources()  # ConfigSource：yaml_defaults / dict / overlay


def build_kernel(
    policies: dict[str, str] | None = None,
    kv: KVStore | None = None,
    config: Config | None = None,
) -> Kernel:
    """把配置装配成内核（api + 真源）。各组件经引用自取依赖、缺省随调用点给出。

    - ``config``：用户配置（两级命名空间），**合并覆盖**到内置默认（:mod:`config.defaults`）之上；
      ``None`` 时纯用内置默认（离线进程内栈）。
    - ``policies``：便捷覆盖运行时策略（折进 ``globals.policies``）。
    - ``kv``：显式注入真源后端，覆盖配置的 kv_store 选择（如传 ``SQLiteKVStore`` 即落盘）。
    """
    _register_all()
    Factory.reset_all()  # 每次组装前清空具名实例缓存以隔离多次装配

    ctx = default_context()
    if config is not None and not config.is_empty():
        ctx = ctx.merged(config.context(known_top_names=Factory.known_top_names()))
    if policies is not None:
        ctx.globals["policies"] = dict(policies)
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

    kv_store = KvProducer.dep(root, default="memory")
    storage = StorageProducer.dep(root, default="composite")
    if not isinstance(storage, Storage):
        raise ValidationError(
            f"storage namespace assembled a non-Storage value: {type(storage).__name__}"
        )

    ingest_jobs = IngestJobProducer.dep(root, default="in_process")
    if not isinstance(ingest_jobs, IngestJobController):
        raise ValidationError(
            "ingest_job namespace assembled a non-IngestJobController value: "
            f"{type(ingest_jobs).__name__}"
        )

    api = LocalMemoryAPI(
        engine=EngineProducer.dep(root, default="in_memory"),
        permission=PermissionProducer.dep(root, default="sqlite"),
        scheduler=SchedulerProducer.dep(root, default="in_process"),
        policy=PolicyProducer.dep(root, default="dict"),
        governor=GovernorProducer.dep(root, default="in_memory"),
        audit_logger=AuditProducer.dep(root, default="sqlite"),
        space=SpaceProducer.dep(root, default="kv"),
        ingest_jobs=ingest_jobs,
    )
    return Kernel(
        api=api,
        kv=kv_store,
        storage=storage,
        ingest_jobs=ingest_jobs,
        space=api.space_manager,
        config_source=config_source,
    )


def assemble(
    policies: dict[str, str] | None = None,
    kv: KVStore | None = None,
    config: Config | None = None,
) -> LocalMemoryAPI:
    """装配出一个可用的 ``MemoryAPI``（见 :func:`build_kernel`）。"""
    return build_kernel(policies, kv, config).api
