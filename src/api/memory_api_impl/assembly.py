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
from importlib import import_module
from typing import Any

from common.audit.base import AuditLogger, AuditProducer
from common.bootstrap import register_plugins
from common.errors import ValidationError
from common.factory.factory import Factory
from common.log import setup_logging
from config import Config
from config.context import ComponentConfig
from config.defaults import KV_DEFAULT_NAME, ROOT_PARAMS, default_context
from construction.bootstrap import register_constructors
from control.bootstrap import register_controllers
from control.engine import EngineProducer
from control.governance import GovernorProducer
from control.permission import PermissionProducer
from control.policy import PolicyProducer
from control.scheduler import SchedulerProducer
from control.space import SpaceManager, SpaceProducer
from ingest.bootstrap import register_ingestors
from retrieval.bootstrap import register_operators
from storage.bootstrap import register_backends
from storage.kv import KvProducer, KVStore

from .local_memory_api import LocalMemoryAPI


@dataclass
class Kernel:
    """一次装配的产物：对外 ``MemoryAPI`` + 真源 kv 句柄（供测试/特殊装配观测真源）。"""

    api: LocalMemoryAPI
    kv: KVStore
    space: SpaceManager | None = None
    audit: AuditLogger | None = None  # 装配好的审计器；surface 侧记认证失败等入口事件


def _register_all() -> None:
    """组装前按层触发自注册（句柄在接口、注册靠 import 实现；各 bootstrap 幂等）。"""
    register_plugins()  # common 共享插件
    register_backends()  # storage
    register_operators()  # retrieval
    register_ingestors()  # ingest
    register_constructors()  # construction
    register_controllers()  # control


# 持久化审计后端：target 名。in_memory 与 sqlite 的 ":memory:" 不算真持久化（重启即丢），
# 不强制 HMAC；真文件 sqlite（db_path 指向文件）需完整性保护。
_PERSISTENT_AUDIT_TARGETS = frozenset({"sqlite"})
_MEMORY_DB_PATHS = frozenset({"", ":memory:"})


def _enforce_audit_integrity(ctx: Any, audit_ref: str) -> None:
    """启动约束（PR③ HMAC 策略）：真文件持久化审计 + 未启用完整性保护 -> 拒绝启动。

    策略（与同事确认）：
    - DEV + 内存审计（in_memory 或 sqlite ``:memory:``）：允许无 HMAC，保调试速度；
    - HMAC 单测/红队：显式配 ``target: hmac``；
    - 生产/生产仿真（真文件持久化 sqlite）+ 未包 HMAC：拒绝启动。

    判定「真文件持久化」：audit target 在持久化后端集合，且 db_path 不是内存占位
    （``:memory:`` 或空）。不据 auth_mode 自动决定--HMAC 由独立配置控制，与认证模式解耦。
    """
    try:
        spec = ctx.lookup(AuditProducer.TOP_NAME, audit_ref)
    except (KeyError, AttributeError):
        # audit 未在配置里显式声明（走 dep 的 default="sqlite" 匿名路径）：
        # 匿名 sqlite 默认 db_path=":memory:"（defaults.py），属内存，不拒。
        return
    # 若配了 hmac，确保其实现已注册（审计 P2-4）：build_kernel 不调 register_security，
    # 故这里单独 import audit_hmac 触发 @AuditProducer.register("hmac")，不连带
    # authenticator 等（PR① 决策2：认证不进 build_kernel）。
    if spec.target == "hmac":
        try:
            import_module("security.audit_hmac")
        except ImportError as exc:
            raise ValidationError(
                "audit 配置了 hmac 但 security.audit_hmac 不可用（缺 security extra？）"
            ) from exc
        return
    if spec.target not in _PERSISTENT_AUDIT_TARGETS:
        return  # in_memory 等非持久化后端
    db_path = str(spec.params.get("db_path", ":memory:"))
    if db_path in _MEMORY_DB_PATHS:
        return  # sqlite 但内存模式，重启即丢，不强制 HMAC
    raise ValidationError(
        f"audit 配置启用了真文件持久化后端（sqlite, db_path={db_path!r}）"
        f"但未启用完整性保护：生产/持久化审计必须包 HmacAuditLogger"
        f"（配 audit.{audit_ref}.target: hmac + inner）。"
        f"DEV/测试用 in_memory 或 sqlite ':memory:' 可跳过。"
    )


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

    # audit logger 装配一次、两处共用：API 内部记业务事件，Kernel.audit 暴露给
    # surface 记入口事件（认证失败等发生在 API 之外，拿不到 API 的私有引用）。
    # 启动约束：真文件持久化审计必须包 HMAC（PR③ 策略）。
    audit_ref = ROOT_PARAMS.get("audit")
    if audit_ref:
        _enforce_audit_integrity(ctx, audit_ref)
    audit_logger = AuditProducer.dep(root, default="sqlite")
    api = LocalMemoryAPI(
        engine=EngineProducer.dep(root, default="in_memory"),
        permission=PermissionProducer.dep(root, default="sqlite"),
        scheduler=SchedulerProducer.dep(root, default="in_process"),
        policy=PolicyProducer.dep(root, default="dict"),
        governor=GovernorProducer.dep(root, default="in_memory"),
        audit_logger=audit_logger,
        space=SpaceProducer.dep(root, default="kv"),
    )
    return Kernel(
        api=api,
        kv=KvProducer.dep(root, default="memory"),
        space=api.space_manager,
        audit=audit_logger,
    )


def assemble(
    policies: dict[str, str] | None = None,
    kv: KVStore | None = None,
    config: Config | None = None,
) -> LocalMemoryAPI:
    """装配出一个可用的 ``MemoryAPI``（见 :func:`build_kernel`）。"""
    return build_kernel(policies, kv, config).api
