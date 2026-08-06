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

from common.audit.base import AuditLogger, AuditProducer
from common.bootstrap import register_plugins
from common.errors import ValidationError
from common.factory.factory import Factory
from common.log import setup_logging
from common.security.authentication.base import AuthProducer
from common.security.authentication.credential_registry import CredentialStatusRegistry
from common.security.authorization import AuthorizationProducer, Authorizer
from common.security.bootstrap import register_security
from config import Config
from config.context import ComponentConfig
from config.defaults import KV_DEFAULT_NAME, ROOT_PARAMS, default_context
from construction.bootstrap import register_constructors
from control.bootstrap import register_controllers
from control.engine import EngineProducer
from control.governance import GovernorProducer
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
    register_security()  # common.security（认证/授权/密码学/防护）
    register_backends()  # storage
    register_operators()  # retrieval
    register_ingestors()  # ingest
    register_constructors()  # construction
    register_controllers()  # control


def _build_authorizer(root: ComponentConfig) -> Authorizer:
    """装配 Authorizer，并挡住把仅测试实现配进生产的装配。

    判据是 :meth:`Authorizer.is_test_only` 这个 capability，不是 ``target == "allow_all"``
    ——第三方注册的恒放行实现同样要被拦住，而核心不认识它的 target 名（S08 不变量 7）。
    单测要用 allow_all 就显式把 ``globals.allow_test_only_security`` 打开，让「这次装配
    不做真实授权」在配置里留下痕迹。
    """
    authorizer = AuthorizationProducer.dep(root, default="standard")
    if not isinstance(authorizer, Authorizer):
        raise ValidationError("authorizer 必须是 Authorizer 实现")
    if authorizer.is_test_only() and not root.get("allow_test_only_security", False):
        raise ValidationError(
            "当前 authorizer 是仅测试实现（恒放行）；生产装配拒绝启动。"
            "确需在测试中使用时显式配置 globals.allow_test_only_security=true"
        )
    return authorizer


def _build_credential_registry(root: ComponentConfig) -> CredentialStatusRegistry:
    """装配凭据撤销复核注册表（PEP 持有，F05 §认证不变量 6、§决策顺序 1）。

    从实际装配的 Authenticator 实例提取 credential_type → KeyStore 映射（P1-1 真源
    统一）：Registry 与 Authenticator 共享同一具名 KeyStore 实例（经 Factory 缓存），
    撤销后 PEP 立即看到。装配时调用所有 KeyStore 的 health() 检查撤销复核 capability
    （P1-2 装配健全性）：第三方 KeyStore 未实现 is_revoked 时启动期拒绝，而非运行期
    500。未配 Authenticator 或全是无 KeyStore 的实现（如 dev）时 Registry 空。

    Round3: 任何已声明 Authenticator 装配失败都拒绝启动（fail-closed），不能静默跳过。
    Round3: 使用 (credential_type, authenticator_name) 复合键，支持平行 Authenticator。
    Round4: 扫描实际装配图，而非仅配置命名空间。从 SecurityRuntime 实例中提取内联
    Authenticator，确保 surface 实际使用的认证器都被注册（P1-2 内联装配支持）。
    """
    from common.security.runtime import SecurityRuntimeProducer

    registry = CredentialStatusRegistry()
    registered: set[tuple[str, str]] = set()  # 避免重复注册同一 (type, name)

    # 第一步：扫描顶层 authenticator 命名空间（独立声明的 Authenticator）
    authenticator_ns = (root.ctx.namespaces or {}).get("authenticator", {})
    for name in authenticator_ns:
        authenticator = AuthProducer.build_named(name, root.ctx)
        if hasattr(authenticator, "key_store"):
            credential_type = authenticator.mode()
            key = (credential_type, name)
            if key not in registered:
                registry.register(credential_type, name, authenticator.key_store)
                registered.add(key)

    # Round4 第二步：扫描所有 SecurityRuntime，提取内联 Authenticator
    # SecurityRuntime 可能在 params.authenticator 中内联 target，这些 Authenticator
    # 不会出现在 authenticator 命名空间，但会被 surface 实际使用。
    security_ns = (root.ctx.namespaces or {}).get("security", {})
    for runtime_name in security_ns:
        # Round5: 移除 try/except，任何装配失败都应该传播（fail-closed）。
        # 配置错误（target 不存在、参数错误）必须在启动期暴露，不能静默跳过。
        runtime = SecurityRuntimeProducer.build_named(runtime_name, root.ctx)
        authenticator = runtime.authenticator
        if hasattr(authenticator, "key_store"):
            credential_type = authenticator.mode()
            # Round5: 内联 Authenticator 的 _name 通常是空字符串（Factory.dep 传递 name=""）
            # 如果是空字符串或 "default"，说明是匿名内联创建的，用 runtime 名称替换。
            auth_name = getattr(authenticator, "_name", "")
            if not auth_name or auth_name == "default":
                # 内联 Authenticator，使用 runtime 名称确保唯一
                auth_name = f"runtime:{runtime_name}"
                # Round5: 修改 Authenticator 的 _name，让它签发的 AuthContext 携带正确 issuer
                if hasattr(authenticator, "_name"):
                    object.__setattr__(authenticator, "_name", auth_name)
            key = (credential_type, auth_name)
            if key not in registered:
                registry.register(credential_type, auth_name, authenticator.key_store)
                registered.add(key)

    # P1-2：装配期健全性检查。Registry.health() 内部调用所有已注册 KeyStore 的
    # health()，确认它们实现了 is_revoked。第三方 Store 漏实现时在此失败，不会等到
    # 首次授权请求才抛 NotImplementedError（F05 §装配不变量「不健康能力启动期拒绝」）。
    registry.health()
    return registry


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
    audit_logger = AuditProducer.dep(root, default="sqlite")
    authorizer = _build_authorizer(root)
    api = LocalMemoryAPI(
        engine=EngineProducer.dep(root, default="in_memory"),
        grant_store=authorizer.management_grant_store(),
        authorizer=authorizer,
        credential_registry=_build_credential_registry(root),
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
