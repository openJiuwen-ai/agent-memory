"""Base surface-server — kernel assembly + the shared verb dispatch.

:class:`Server` is the **base class** every protocol surface builds on: it holds
one assembled kernel (config + api + truth-source) and exposes :meth:`dispatch`,
the verb router that the CLI and HTTP/MCP surfaces all share. A concrete surface
subclasses it and adds its transport (see :class:`bootstrap.http_server.__main__.HttpServer`
for the HTTP/socket surface); the CLI's ``InProcessClient`` uses the base directly.

The minimal reference build uses :func:`api.build_kernel` (the per-capability
impls wired together, pure in-memory, no external deps). Swapping in a real profile
means assembling real plugins/Stores in :meth:`build` and reusing the same
``dispatch``.

本模块仍按 flat import root 使用（``import server``）。Docker 镜像通过 editable
安装、本地启动脚本通过 ``PYTHONPATH`` 保证仓库 ``src/`` 的导入优先级；这里仅在
直接运行且未配置环境时把 ``src/`` 追加为兜底路径。
"""

from __future__ import annotations

import logging
import os
import sys
from importlib import import_module
from typing import Any, Dict, Tuple

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"
)
if _SRC not in sys.path:
    # 直接运行且未做 editable 安装/PYTHONPATH 配置时的兜底。导入优先级由 Docker
    # editable 安装或 scripts/run-*.sh 保证，避免运行时把路径强插到最前。
    sys.path.append(_SRC)

Config = import_module("profiles").Config

_api_module = import_module("api")
Kernel = _api_module.Kernel
build_kernel = _api_module.build_kernel
KernelConfig = import_module("config").Config
Factory = import_module("common.factory.factory").Factory
AuthProducer = import_module("common.authentication.base").AuthProducer
RateLimitProducer = import_module("common.admission.base").RateLimitProducer
register_plugins = import_module("common.bootstrap").register_plugins
ValidationError = import_module("common.errors").ValidationError

_LOG = logging.getLogger(__name__)


class Server:
    """Assembled kernel + shared dispatch; base for all protocol surfaces."""

    def __init__(
        self,
        config: Config,
        kernel: Kernel,
        authenticator: Any = None,
        rate_limiter: Any = None,
        argon2_guard: Any = None,
    ) -> None:
        self.config = config
        self.kernel = kernel
        self.authenticator = authenticator
        self.rate_limiter = rate_limiter
        self.argon2_guard = argon2_guard

    @property
    def api(self):
        return self.kernel.api

    @property
    def kv(self):
        return self.kernel.kv

    @property
    def audit(self):
        """装配好的审计器（可能为 None）——认证中间件记入口事件用。"""
        return self.kernel.audit

    # ``rate_limiter`` 只在有网络对端的 surface（HTTP）传给中间件：进程内直连与
    # MCP stdio 没有远端，限流无对象可分桶，见 ``common.admission.RateLimiter.allow``。

    @classmethod
    def build(cls, config: Config, spaces: Any = None) -> "Server":
        """Assemble a kernel from ``config`` and return a ``cls`` instance.

        ``config.settings`` 是合并后的完整配置字典，含 profiles 层自有的 ``profile`` /
        ``policies`` 等顶层键；其中 ``memory_api`` 段（若有）才是交给内核的**两级命名空间**
        装配配置，解析为 :class:`~config.Config` 后由 :func:`api.build_kernel` 合并覆盖到
        内置默认之上装配出接真后端的内核。须**只取该段**交 ``from_dict`` —— 整包传入会让
        ``profile`` / ``policies`` 撞上新配置解析期的顶层段名校验而报错。无该段时（纯
        ``OFFLINE`` 档）``from_dict(None)`` 返回空配置，回落进程内默认实现，与原行为一致。
        """
        # 必须在 from_dict 之前：authenticator / key_store 两个顶层段名要先进
        # Factory.known_top_names()，否则配置解析期会把它们当未知段拒掉。
        register_plugins()
        kernel_config = KernelConfig.from_dict(config.settings.get("memory_api"))
        kernel = build_kernel(policies=config.policies or None, config=kernel_config)
        authenticator = _build_authenticator(kernel_config)
        rate_limiter = _build_rate_limiter(kernel_config, authenticator)
        argon2_guard = _build_argon2_guard(kernel_config, authenticator)
        return cls(config, kernel, authenticator, rate_limiter, argon2_guard)

    def dispatch(self, verb: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Route a ``(verb, payload)`` through the shared handler."""
        from handler import dispatch as _dispatch

        return _dispatch(self, verb, payload)


def _build_authenticator(kernel_config: Any):
    """按配置的 ``authenticator`` 段装配认证器；无该段时回落 DEV 并警告。

    回落到 DEV（而非拒绝启动）是刻意的：不打断任何人的本地开发。第一期只是把
    「无认证」从**隐式且不可改**变成**显式、可切换、且非 localhost 时拒绝启动**
    （DEV 的绑定 guard 见 ``common.authentication.check_dev_binding``）。
    """
    ctx = kernel_config.context(known_top_names=Factory.known_top_names())
    names = sorted(ctx.namespaces.get(AuthProducer.TOP_NAME, {}))
    if not names:
        _LOG.warning(
            "未配置 authenticator 段，回落 DEV 模式：所有请求以 ROOT 身份放行。"
            "生产部署须显式配置 authenticator.default.target 为 api_key 或 trusted。"
        )
        return AuthProducer.build("dev", {}, ctx)
    name = _select_configured_instance(AuthProducer.TOP_NAME, names)
    return AuthProducer.build_named(name, ctx)


def _build_rate_limiter(kernel_config: Any, authenticator: Any):
    """按配置的 ``rate_limiter`` 段装配限流器；无该段时按认证 capability 给默认。

    **默认按 capability 分岔**（§8.1）：仅限 loopback 的实现不限流，其余实现默认开
    ``token_bucket``。
    理由是限流保护的具体对象——Argon2id verify——只在 API_KEY 模式下存在；DEV
    模式已被强制绑定 localhost（``common.authentication.check_dev_binding``），没有远端
    攻击面，此时限流只会把本地压测和调试脚本卡住，是纯粹的开发阻碍。

    非 DEV 默认**开**而不是默认关：默认关等于「必须读过 §8.1 才知道要配」，
    而没配的后果是一个能打挂进程的可用性漏洞。默认开的代价是运维可能撞上 429，
    但那会伴随一个明确的状态码和一个明确的配置项；默认关的代价是没有信号。

    网关后部署（TRUSTED 模式的常见形态）所有请求共用网关出口 IP，会被当成
    同一个 peer——这种部署应显式配 ``target: unlimited`` 把限流交给网关，
    或按聚合流量调大 ``capacity``。见 F01 归档文档的「破坏性变更」。
    """
    ctx = kernel_config.context(known_top_names=Factory.known_top_names())
    names = sorted(ctx.namespaces.get(RateLimitProducer.TOP_NAME, {}))
    if names:
        name = _select_configured_instance(RateLimitProducer.TOP_NAME, names)
        return RateLimitProducer.build_named(name, ctx)

    if authenticator.requires_loopback_binding():
        return RateLimitProducer.build("unlimited", {}, ctx)
    return RateLimitProducer.build("token_bucket", {}, ctx)


def _build_argon2_guard(kernel_config: Any, authenticator: Any):
    """认证重型校验的进程级并发上限（当前实现为 Argon2 guard）。

    是否装配由 ``Authenticator.requires_concurrency_guard`` 声明，不再按封闭枚举分支。
    API_KEY 返回 True；TRUSTED/DEV 返回 False；第三方实现默认 True（fail closed）。

    上限由 ``argon2.max_concurrent`` 配置（默认 4，按 512 MiB / 128 MiB 算）。
    """
    if not authenticator.requires_concurrency_guard():
        return None
    settings = (
        kernel_config.settings.get("argon2", {}) if hasattr(kernel_config, "settings") else {}
    )
    max_concurrent = int(settings.get("max_concurrent", 4)) if settings else 4
    _guard_mod = import_module("common.admission.concurrency_guard")
    return _guard_mod.default_argon2_guard(max_concurrent=max_concurrent)


def _select_configured_instance(top_name: str, names: list[str]) -> str:
    """选定安全组件实例；多实例无 ``default`` 时拒绝歧义配置。"""
    if "default" in names:
        return "default"
    if len(names) == 1:
        return names[0]
    raise ValidationError(
        f"{top_name} 定义了多个具名实例 {names!r}，但未定义 'default'；"
        "安全组件选择存在歧义，拒绝启动。"
    )


def default_spaces() -> Dict[str, Any]:
    """Default scope/namespace registry (none needed for the in-memory build)."""
    return {}


def build(config: Config, spaces: Any = None) -> Server:
    """Module-level assembly shim (the CLI's ``InProcessClient`` calls this)."""
    return Server.build(config, spaces)
