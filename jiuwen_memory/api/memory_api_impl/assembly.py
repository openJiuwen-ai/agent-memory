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

注册靠 import 各实现模块触发，组装前由各层 bootstrap 统一拉起（句柄住在接口层）。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.audit.base import AuditLogger, AuditProducer
from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.lock import LockProducer
from jiuwen_memory.common.log import get_logger, setup_logging
from jiuwen_memory.common.security._assembly_lifecycle import _assembly_lifecycle
from jiuwen_memory.common.security.audit_integrity.base import (
    DEFAULT_AUDIT_VERIFY_MAX_SAMPLES,
    DEFAULT_AUDIT_VERIFY_PAGE_SIZE,
    AuditVerificationLimits,
)
from jiuwen_memory.common.security.authentication.credential_registry import (
    CredentialStatusRegistry,
)
from jiuwen_memory.common.security.authorization.base import (
    AuthorizationProducer,
    Authorizer,
)
from jiuwen_memory.common.security.capabilities import (
    CloseableSecurityCapability,
    SecurityLifecycleCapability,
    SecurityResourceOwner,
    needs_space_facts,
    share_security_lifecycle,
    validate_authorization_capabilities,
)
from jiuwen_memory.common.security.runtime import SecurityRuntime
from jiuwen_memory.common.security.types import RequestSecurityContext
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import Config
from jiuwen_memory.config.config_source import ConfigSource, ConfigSourceProducer
from jiuwen_memory.config.config_source_impl import register_config_sources
from jiuwen_memory.config.context import AssemblyContext, ComponentConfig
from jiuwen_memory.config.defaults import KV_DEFAULT_NAME, ROOT_PARAMS, default_context
from jiuwen_memory.config.document_flag import (
    WATCH_DOCUMENT_KEY,
    resolve_watch_document,
)
from jiuwen_memory.construction.bootstrap import register_constructors
from jiuwen_memory.construction.router import optional_router
from jiuwen_memory.control.bootstrap import register_controllers
from jiuwen_memory.control.engine import EngineProducer
from jiuwen_memory.control.governance import GovernorProducer
from jiuwen_memory.control.ingest_job import IngestJobController, IngestJobProducer
from jiuwen_memory.control.membership import MembershipProducer
from jiuwen_memory.control.permission import PermissionManager, PermissionProducer
from jiuwen_memory.control.policy import PolicyProducer
from jiuwen_memory.control.scheduler import Scheduler, SchedulerProducer
from jiuwen_memory.control.space import SpaceManager, SpaceProducer
from jiuwen_memory.control.types import BackgroundJobStartResult
from jiuwen_memory.ingest.bootstrap import register_ingestors
from jiuwen_memory.retrieval.bootstrap import register_operators
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.store_manager import (
    StoreManager,
    StoreManagerProducer,
    resolve_name,
)
from jiuwen_memory.storage.watchdog import Watchdog, WatchdogProducer

from ..access_security import _build_security_runtime
from ..memory_api import MemoryAPI
from .local_memory_api import LocalMemoryAPI

logger = get_logger(__name__)

AUTO_SECURITY_RUNTIME = object()

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
        audit: 装配好的审计器；供 surface 记录发生在 API 外的认证事件
        config_source: 运行时晚绑定配置来源（默认 YamlDefaultsConfigSource）
    """

    api: LocalMemoryAPI
    kv: KVStore
    storage: StoreManager
    ingest_jobs: IngestJobController
    space: SpaceManager | None = None
    audit: AuditLogger | None = None
    config_source: ConfigSource | None = None
    scheduler: Scheduler | None = None
    watchdog: Watchdog | None = None
    security_runtime: SecurityRuntime | None = None
    security_resources: SecurityResourceOwner | None = None

    async def start_background(self) -> None:
        """启动需事件循环就绪的后台组件。

        ``build_kernel`` 是同步函数，装配期无 running loop，故看门狗的 ``start``
        （内部 ``asyncio.get_running_loop`` 取 loop 绑 Observer 桥接）延后到此。
        由 server startup event 调用。无 watchdog 时为 no-op。
        """
        if self.watchdog is not None:
            self.watchdog.start()
            # 与 LocalWatchdog.start 的 info 呼应，确认"确实调了 start"——
            # close 时若发现"装配但未 start"，本日志可回溯定位漏调点。
            logger.info("start_background: watchdog.start() called")

    def close(self) -> None:
        """后台屏障成功后，统一释放自有 shadow 与安全资源。"""
        if self.security_runtime is not None:
            self.security_runtime.close()
        elif self.security_resources is not None:
            self.security_resources.close()
        elif self.storage.has_shadow_index():
            shadow = self.storage.shadow_index()
            if shadow is not None:
                shadow.close()


@runtime_checkable
class MemoryRuntime(Protocol):
    """Access composition root 使用的运行时句柄：API + 生命周期语义。

    ``start_background``（异步面，watchdog 绑当前 loop）与 ``start``（同步面，daemon
    线程自持 loop）由各协议面按自己的并发模型择一显式调；不调则文档看门狗不启动。
    """

    @property
    def api(self) -> MemoryAPI:
        """返回运行时的公共 API。"""
        ...

    async def start_background_jobs(
        self, scope: Scope, *, security: RequestSecurityContext,
    ) -> BackgroundJobStartResult:
        """注册固定 home 的自动建树；需开启树与自动开关及支持周期的调度器。"""
        ...

    async def start_background(self) -> None:
        """Bind optional background components to the caller's long-lived event loop."""
        ...

    def start(self) -> None:
        """Start optional background components on a Runtime-owned event loop."""
        ...

    def close(self, *, wait: bool = True) -> None:
        """幂等关闭；wait=False 后台收尾，wait=True 等待同一清理尝试。

        后台任务和状态写入结束后才释放依赖；等待关闭可取得后台清理异常。
        """
        ...


@runtime_checkable
class ServerRuntime(MemoryRuntime, Protocol):
    """公开 Server 装配结果；安全绑定与资源所有权已在内核完成。"""

    @property
    def security_runtime(self) -> Any:
        """返回入口认证/保护能力组合；未选择认证时为 None。"""
        ...

    @property
    def audit(self) -> AuditLogger | None:
        """返回与 API 业务审计同源的入口审计器。"""
        ...


@dataclass(frozen=True)
class _ServerRuntime:
    _runtime: MemoryRuntime
    security_runtime: Any
    audit: AuditLogger | None

    @property
    def api(self) -> MemoryAPI:
        return self._runtime.api

    async def start_background_jobs(
        self, scope: Scope, *, security: RequestSecurityContext,
    ) -> BackgroundJobStartResult:
        return await self._runtime.start_background_jobs(scope, security=security)

    async def start_background(self) -> None:
        await self._runtime.start_background()

    def start(self) -> None:
        self._runtime.start()

    def close(self, *, wait: bool = True) -> None:
        self._runtime.close(wait=wait)


@dataclass
class _MemoryRuntime:
    """Runtime 生命周期：后台使用者全部停止之后才释放共享依赖。"""

    api: MemoryAPI
    _kernel: _Kernel
    _resources: SecurityResourceOwner | None = field(default=None, repr=False)
    _shutdown_lock: Any = field(default_factory=threading.RLock, init=False, repr=False)
    _drain_lock: Any = field(default_factory=threading.Lock, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)
    _drain_attempted: bool = field(default=False, init=False, repr=False)
    _background_drained: bool = field(default=False, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _loop_thread: threading.Thread | None = field(default=None, repr=False)
    _close_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _close_error: BaseException | None = field(default=None, init=False, repr=False)
    _close_errors: list[BaseException] = field(default_factory=list, init=False, repr=False)
    _watchdog_stopped: bool = field(default=True, init=False, repr=False)

    async def start_background_jobs(
        self, scope: Scope, *, security: RequestSecurityContext,
    ) -> BackgroundJobStartResult:
        """注册工作委托现有 Scheduler，自有循环与关闭屏障保持统一。"""
        self._validate_resources()
        with self._shutdown_lock:
            self._validate_start()
        result = await self._kernel.api.start_background_jobs(scope, security=security)
        with self._shutdown_lock:
            closed = self._closing
        if closed:
            for job_id in result.job_ids:
                self._kernel.scheduler.cancel(job_id)
            raise ValidationError("runtime closed during periodic registration")
        return result

    async def start_background(self) -> None:
        """在异步宿主的长寿循环中启动；关闭后拒绝重启。"""
        self._validate_resources()
        with self._shutdown_lock:
            self._validate_start()
            await self._kernel.start_background()

    def start(self) -> None:
        """同步宿主使用自有循环；启动异常传回调用方。"""
        self._validate_resources()
        with self._shutdown_lock:
            self._validate_start()
            self._start_loop()

    def _validate_start(self) -> None:
        if self._closing:
            raise ValidationError("cannot start a closed MemoryRuntime")

    def _validate_resources(self) -> None:
        if self._resources is not None and self._resources.is_closed():
            raise ValidationError("cannot start a closed MemoryRuntime")

    def _start_loop(self) -> None:
        if self._kernel.watchdog is None or self._loop_thread is not None:
            return
        started = threading.Event()
        loop = asyncio.new_event_loop()
        self._loop = loop
        errors: list[BaseException] = []

        def run() -> None:
            asyncio.set_event_loop(loop)
            try:
                try:
                    loop.run_until_complete(self._kernel.start_background())
                except BaseException as exc:
                    errors.append(exc)
                    return
                finally:
                    started.set()
                loop.run_forever()
            finally:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_default_executor())
                loop.close()

        self._loop_thread = threading.Thread(target=run, name="memory-runtime-loop", daemon=True)
        self._loop_thread.start()
        started.wait()
        if errors:
            self._loop_thread.join()
            self._loop_thread = None
            self._loop = None
            raise errors[0]

    def _validate_shutdown(self, *, wait: bool) -> None:
        if wait and threading.current_thread() is self._loop_thread:
            raise RuntimeError("cannot wait for MemoryRuntime from its own loop thread")
        if self._kernel.scheduler is not None:
            self._kernel.scheduler.validate_shutdown(wait=wait)

    def close(self, *, wait: bool = True) -> None:
        """False 启动单个后台收尾；True 等待同一清理尝试并报告异常。"""
        self._validate_shutdown(wait=wait)
        # 根锁先于 Runtime 锁，避免与外层 SecurityRuntime 的关闭屏障形成锁反转。
        if self._resources is not None:
            self._resources.begin_close()
        with self._shutdown_lock:
            self._request_stop()
            if self._close_thread is None:
                self._close_thread = threading.Thread(
                    target=self._finish_close, name="memory-runtime-close", daemon=True
                )
                self._close_thread.start()
            thread = self._close_thread
        if wait:
            thread.join()
            if self._close_error is not None:
                raise self._close_error

    def _request_stop(self) -> None:
        with self._shutdown_lock:
            if self._closing:
                return
            self._closing = True
            if self._kernel.watchdog is not None:
                self._watchdog_stopped = self._close_step(
                    "watchdog.stop", self._kernel.watchdog.stop
                )
            if self._kernel.scheduler is not None:
                self._close_step(
                    "scheduler.shutdown(wait=False)",
                    lambda: self._kernel.scheduler.shutdown(wait=False),
                )
            self._close_step(
                "ingest_jobs.close(wait=False)", lambda: self._kernel.ingest_jobs.close(wait=False)
            )

    def _close_step(self, name: str, action: Callable[[], None]) -> bool:
        try:
            action()
        except BaseException as exc:
            exc.add_note(f"MemoryRuntime.close step: {name}")
            self._close_errors.append(exc)
            logger.exception("MemoryRuntime.close failed at %s", name)
            return False
        return True

    def _drain_background(self) -> bool:
        self._request_stop()
        with self._drain_lock:
            if self._drain_attempted:
                return self._background_drained
            self._drain_attempted = True
            if not self._watchdog_stopped and self._kernel.watchdog is not None:
                self._watchdog_stopped = self._close_step(
                    "watchdog.stop retry", self._kernel.watchdog.stop
                )
            ingest_stopped = self._close_step(
                "ingest_jobs.close(wait=True)", lambda: self._kernel.ingest_jobs.close(wait=True)
            )
            scheduler_stopped = True
            if self._kernel.scheduler is not None:
                scheduler_stopped = self._close_step(
                    "scheduler.shutdown(wait=True)",
                    lambda: self._kernel.scheduler.shutdown(wait=True),
                )
            stopped = self._watchdog_stopped and ingest_stopped and scheduler_stopped
            if stopped:
                # 锁释放失败不阻断已安全的资源清理；停止不确定时不释放锁或资源。
                release = getattr(self.api, "release_dreaming_lock", None)
                if callable(release):
                    self._close_step("release_dreaming_lock", release)
            loop_stopped = self._stop_loop() if self._watchdog_stopped else False
            self._background_drained = stopped and loop_stopped
            return self._background_drained

    def _stop_loop(self) -> bool:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            if not self._close_step(
                "runtime loop.stop", lambda: loop.call_soon_threadsafe(loop.stop)
            ):
                return False
        if self._loop_thread is not None:
            if not self._close_step("runtime loop.join", self._loop_thread.join):
                return False
        self._loop = None
        self._loop_thread = None
        return True

    def _stop_for_shared_close(self) -> None:
        """只等待后台屏障，不 join 收尾线程（它可能正在等资源根锁）。"""
        self._validate_shutdown(wait=True)
        if not self._drain_background():
            raise BaseExceptionGroup("MemoryRuntime background stop failed", self._close_errors)

    def _finish_close(self) -> None:
        try:
            if self._drain_background():
                self._close_step("kernel.close", self._kernel.close)
        except BaseException as exc:
            self._close_errors.append(exc)
            logger.exception("MemoryRuntime.close encountered an unexpected failure")
        finally:
            if len(self._close_errors) == 1:
                self._close_error = self._close_errors[0]
            elif self._close_errors:
                self._close_error = BaseExceptionGroup(
                    "MemoryRuntime.close failed", self._close_errors
                )

    def security_resource_owner(self) -> SecurityResourceOwner:
        if self._resources is None:
            raise ValidationError("MemoryRuntime has no security resource owner")
        return self._resources


def _coerce_config(config: Config | Mapping[str, Any] | None) -> Config | None:
    """Access 只传 dict / None；内核测试仍可传 ``Config``。"""
    if config is None or isinstance(config, Config):
        return config
    if isinstance(config, Mapping):
        return Config.from_dict(config)
    raise TypeError(
        f"assemble config must be Config, mapping, or None, got {type(config).__name__}"
    )


def _register_all() -> None:
    """组装前按层触发自注册（句柄在接口、注册靠 import 实现；各 bootstrap 幂等）。"""
    register_plugins()  # common 共享插件
    register_backends()  # storage
    register_operators()  # retrieval
    register_ingestors()  # ingest
    register_constructors()  # construction
    register_controllers()  # control
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
    with _assembly_lifecycle() as lifecycle:
        kernel = _assemble_kernel(policies, kv, config)
        owner = (
            kernel.security_runtime.security_resource_owner()
            if kernel.security_runtime is not None
            else kernel.security_resources
        )
        lifecycle.bind(owner)
        return kernel


def _assemble_kernel(
    policies: dict[str, str] | None,
    kv: KVStore | None,
    config: Config | None,
) -> _Kernel:
    """在创建观察作用域内解析依赖，失败由外层按本次拥有关系回滚。"""

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

    # API 业务审计与 surface 入口审计必须共用同一个具名实例。
    audit_logger = AuditProducer.dep(root, default="sqlite")

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
    scheduler = SchedulerProducer.dep(root, default="in_process")
    with _assembly_lifecycle() as lifecycle:
        lifecycle.failure_cleanup.append(scheduler.shutdown)
    lock_specs = root.ctx.namespaces.get(LockProducer.TOP_NAME, {})
    dreaming_lock = (
        LockProducer.build_named("dreaming", root.ctx) if "dreaming" in lock_specs else None
    )

    authorizer = AuthorizationProducer.dep(root, default="standard")
    if not isinstance(authorizer, Authorizer):
        raise ValidationError(
            f"authorizer namespace assembled a non-Authorizer value: {type(authorizer).__name__}"
        )
    _reject_test_only_authorizer(authorizer, config)
    required = ctx.globals.get("require_grant_management", False)
    if not isinstance(required, bool):
        raise ValidationError("require_grant_management must be bool")
    validate_authorization_capabilities(authorizer, required=required)

    # 在同一装配纪元建立 Registry，随后从实际认证器绑定签发真源；SDK 与 Server 同路。
    credential_registry = CredentialStatusRegistry()
    engine = EngineProducer.dep(root, default="in_memory")
    permission = PermissionProducer.dep(root, default="sqlite")

    api = LocalMemoryAPI(
        engine=engine,
        permission=permission,
        scheduler=scheduler,
        dreaming_lock=dreaming_lock,
        policy=PolicyProducer.dep(root, default="dict"),
        governor=GovernorProducer.dep(root, default="in_memory"),
        audit_logger=audit_logger,
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
        authorizer=authorizer,
        credential_registry=credential_registry,
    )
    _reject_routing_without_space_authorization(api)
    watchdog: Watchdog | None = None
    has_shadow = storage.has_shadow_index()
    watch_doc = resolve_watch_document(root.get(WATCH_DOCUMENT_KEY, True))
    if has_shadow and watch_doc:
        watchdog = WatchdogProducer.dep(root, default="watchdog")
    # "文档看门狗是否启用"的唯一决策点：shadow 端口缺失或 watch_document 关闭
    # 都会让 assembled=False，此时 md 手改不会被增量同步——出问题时据本日志定位。
    logger.info(
        "watchdog assembled=%s (shadow=%s, watch_doc=%s)",
        watchdog is not None,
        has_shadow,
        watch_doc,
    )

    # 同包 PEP 装配：最终 PDP 替换仍须满足本部署的完整管理要求。
    api._bind_authorizer(authorizer, required=required)  # pylint: disable=protected-access
    _warn_inert_permission_namespace(permission, api, config)
    security_runtime = _build_security_runtime(ctx)
    if security_runtime is not None:
        if security_runtime.authorizer is not authorizer:
            raise ValidationError("security Runtime 与内核 PEP 的 authorizer 必须是同一实例")
        # 同包装配接缝；不为初始化凭据真源扩充冻结的 MemoryAPI。
        api._bind_credential_sources(security_runtime.authenticator)  # pylint: disable=protected-access
    shadow_names = {"default", *ctx.namespaces.get("shadow_index", {})}
    shadows = []
    for name in sorted(shadow_names):
        if storage.has_shadow_index(name):
            shadows.append(storage.shadow_index(name))
    resources = SecurityResourceOwner((authorizer, *shadows))
    if security_runtime is not None:
        resources.share_with(security_runtime.security_resource_owner())
    return _Kernel(
        api=api,
        kv=kv_store,
        storage=storage,
        ingest_jobs=ingest_jobs,
        space=space,
        audit=audit_logger,
        config_source=config_source,
        scheduler=scheduler,
        watchdog=watchdog,
        security_runtime=security_runtime,
        security_resources=resources,
    )


def _reject_test_only_authorizer(authorizer: Authorizer, config: Config | None) -> None:
    """Reject a test-only PDP from an explicitly secured production assembly."""
    if not authorizer.is_test_only():
        return
    if config is None or config.is_empty():
        return
    if not config.context(known_top_names=Factory.known_top_names()).namespaces.get("security"):
        return
    raise ValidationError(
        "配置了 security 段（生产装配）但 authorizer 是仅限测试的恒放行实现。"
        "请把 authorizer.default.target 配成 standard（或 routing）并配齐 grant_store "
        "与 delegation_store。"
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
        "把 authorizer.default.target 配成 space_aware（params.delegate 指向具名 "
        "standard），或删掉 router 命名空间。"
        "原因：判定表把写入分流进协作空间，而当前的判定实现不读空间事实，"
        "这些协作空间没有任何权限边界——写入成功、检索也拿得到，只是拿得到的人多了。"
    )


def _warn_inert_permission_namespace(
    permission: PermissionManager, api: LocalMemoryAPI, config: Config | None
) -> None:
    """permission 段与 authorizer 的空间语义一致性守卫。

    空间级判定已收敛到 authorizer（F04 §3.1），permission 段在内容读写路径上不再
    被调用。两个处置分支：

    - permission 段显式选了声明 ``requires_space_facts`` 的管理器（如 space_aware），
      而 authorizer 不提供空间语义：**拒绝启动**。该组合意味着调用方以为装了空间
      判定，实际空间级入口整体回落标准链——成员记录、归属主体档与两轴求值全部
      静默失效，失效方向是放宽。
    - 其余显式选择的 permission 实现不被调用：降为 WARNING，方向不构成越权。
    """
    if config is None or config.is_empty():
        return
    if not config.context(known_top_names=Factory.known_top_names()).namespaces.get("permission"):
        return
    space_authorization = api.space_governance_enabled
    if needs_space_facts(permission) and not space_authorization:
        raise ValidationError(
            "permission 段选了 space_aware 判定，但 authorizer 不提供空间语义"
            "（requires_space_facts() 为假）。空间级入口会整体回落标准链，"
            "成员记录与归属主体档静默失效。请把 authorizer.default.target 配成 "
            "space_aware（params.delegate 指向具名 standard），或改回非空间判定。"
        )
    if space_authorization:
        return
    logger.warning(
        "配置了 permission 段，但该判定实现不再处于内容读写路径——"
        "空间级与主体面判定均由 authorizer 终局（不变量 23）。"
    )


def assemble(
    policies: dict[str, str] | None = None,
    kv: KVStore | None = None,
    config: Config | Mapping[str, Any] | None = None,
) -> MemoryAPI:
    """装配出一个可用的 ``MemoryAPI``。普通调用方只应使用本函数。"""
    return assemble_runtime(policies, kv, config).api


def assemble_runtime(
    policies: dict[str, str] | None = None,
    kv: KVStore | None = None,
    config: Config | Mapping[str, Any] | None = None,
) -> MemoryRuntime:
    """装配 Access 运行时：``api`` + ``close``，不暴露存储或任务控制器端口。"""
    kernel = _build_kernel(policies, kv, config)
    return _runtime_from_kernel(kernel)


def _runtime_from_kernel(kernel: _Kernel) -> _MemoryRuntime:
    """私有装配工作对象转换为公共生命周期句柄。"""
    runtime = _MemoryRuntime(
        api=kernel.api,
        _kernel=kernel,
        _resources=kernel.security_resources,
    )
    # 本包 facade 持有委托闭包，不将 Runtime/Store 端口公开给业务层。
    # pylint: disable=protected-access
    kernel.api._runtime_close = runtime.close
    runtime.security_resource_owner().add_close_barrier(runtime._stop_for_shared_close)
    return runtime


def assemble_server_runtime(
    policies: dict[str, str] | None = None,
    config: Config | Mapping[str, Any] | None = None,
    *,
    security_runtime: Any = AUTO_SECURITY_RUNTIME,
) -> ServerRuntime:
    """装配 Server 门面，Access 无需访问 MemoryRuntime/API 的私有成员。

    默认复用配置的安全 Runtime，显式 None 不选择入口认证；显式组合覆盖入口能力，
    若包含 PDP/Authenticator 则在内核回绑并校验。成功返回后统一负责已选择能力的
    生命周期；失败只关闭本次内核，不接管调用方注入对象。
    """
    kernel = _build_kernel(policies=policies, config=config)
    runtime = _runtime_from_kernel(kernel)
    try:
        auto_security = security_runtime is AUTO_SECURITY_RUNTIME
        selected = kernel.security_runtime if auto_security else security_runtime
        _bind_server_security(kernel, selected, auto_security=auto_security)
        facade = _ServerRuntime(runtime, selected, kernel.audit)
        share_security_lifecycle(runtime, selected)
        if isinstance(selected, CloseableSecurityCapability) and not isinstance(
            selected, SecurityLifecycleCapability
        ):
            runtime.security_resource_owner().share_with(SecurityResourceOwner((selected,)))
        # 同包 facade 委托统一关闭，不将安全装配接缝交给 Access。
        kernel.api._runtime_close = facade.close  # pylint: disable=protected-access
        return facade
    except BaseException as assembly_error:
        try:
            runtime.close(wait=True)
        except BaseException as cleanup_error:
            raise assembly_error from cleanup_error
        raise


def _bind_server_security(kernel: _Kernel, selected: Any, *, auto_security: bool) -> None:
    """同包内完成 PDP、认证签发源的最终调和，保留旧部分能力组合适配。"""
    if selected is None:
        return
    # SecurityRuntime 的公开健康契约；旧部分组合没有整体 health，仍逐项校验绑定能力。
    health = getattr(selected, "health", None)
    if callable(health):
        health()
    authorizer = getattr(selected, "authorizer", None)
    if authorizer is not None:
        # AUTO 的身份一致性已在 _assemble_kernel 发布前校验。
        if authorizer.is_test_only():
            raise ValidationError(
                "Server refuses a test-only authorizer; configure a production PDP"
            )
        if not auto_security:
            # LocalMemoryAPI 私有接缝只允许本包的装配器使用。
            kernel.api._bind_authorizer(authorizer)  # pylint: disable=protected-access
    authenticator = getattr(selected, "authenticator", None)
    if authenticator is not None:
        kernel.api._bind_credential_sources(authenticator)  # pylint: disable=protected-access
