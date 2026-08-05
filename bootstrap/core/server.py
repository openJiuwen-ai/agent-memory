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
SecurityRuntimeProducer = import_module("common.security.runtime").SecurityRuntimeProducer
register_plugins = import_module("common.bootstrap").register_plugins
ValidationError = import_module("common.errors").ValidationError

_LOG = logging.getLogger(__name__)


class Server:
    """Assembled kernel + shared dispatch; base for all protocol surfaces."""

    def __init__(
        self,
        config: Config,
        kernel: Kernel,
        security: Any = None,
    ) -> None:
        self.config = config
        self.kernel = kernel
        self.security = security

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

    @property
    def authenticator(self):
        return self.security.authenticator if self.security is not None else None

    @property
    def rate_limiter(self):
        return self.security.rate_limiter if self.security is not None else None

    @property
    def workload_guard(self):
        """昂贵认证操作的并发预算；认证实现声明不需要时为 ``None``。"""
        if self.security is None:
            return None
        if not self.security.authenticator.requires_concurrency_guard():
            return None
        return self.security.workload_guard

    @property
    def binding_policy(self):
        return self.security.binding_policy if self.security is not None else None

    # ``rate_limiter`` 只在有网络对端的 surface（HTTP）传给中间件：进程内直连与
    # MCP stdio 没有远端，限流无对象可分桶，见 ``common.security.protection.RateLimiter.allow``。

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
        # 必须在 from_dict 之前：security / authenticator / key_store 等顶层段名要先进
        # Factory.known_top_names()，否则配置解析期会把它们当未知段拒掉。
        register_plugins()
        kernel_config = KernelConfig.from_dict(config.settings.get("memory_api"))
        kernel = build_kernel(policies=config.policies or None, config=kernel_config)
        security = _build_security(kernel_config)
        return cls(config, kernel, security)

    def dispatch(
        self, verb: str, payload: Dict[str, Any], security: Any = None
    ) -> Tuple[int, Dict[str, Any]]:
        """Route a ``(verb, payload)`` through the shared handler.

        ``security`` 是中间件产出的 ``RequestSecurityContext``；缺失时 handler 返回
        401（fail-closed），本层不代为构造。
        """
        from handler import dispatch as _dispatch

        return _dispatch(self, verb, payload, security)


def _build_security(kernel_config: Any):
    """按配置的 ``security`` 段装配 :class:`SecurityRuntime`；无该段时回落 DEV 并警告。

    回落到 DEV（而非拒绝启动）是刻意的：不打断任何人的本地开发。它把「无认证」
    从**隐式且不可改**变成**显式、可切换、且非 loopback 时拒绝启动**——DEV 的绑定
    约束由 Runtime 的 ``binding_policy`` 在 socket 绑定前执行（F05 §Protection
    §BindingPolicy）。

    装配完成后立刻 ``health()``：能力不健康必须在启动期拒绝，不能等到第一个请求
    打进来才在 500 里暴露（F05 §默认拒绝）。
    """
    ctx = kernel_config.context(known_top_names=Factory.known_top_names())
    names = sorted(ctx.namespaces.get(SecurityRuntimeProducer.TOP_NAME, {}))
    if not names:
        _LOG.warning(
            "未配置 security 段，回落 DEV 模式：所有请求以 ROOT 身份放行。"
            "生产部署须显式配置 security.default 并把 authenticator 指向 api_key 或 trusted。"
        )
        runtime = SecurityRuntimeProducer.build(
            "standard", {"authenticator": {"target": "dev"}}, ctx
        )
    else:
        name = _select_configured_instance(SecurityRuntimeProducer.TOP_NAME, names)
        runtime = SecurityRuntimeProducer.build_named(name, ctx)
    runtime.health()
    return runtime


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
