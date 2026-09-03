# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Base surface-server — kernel assembly + the shared verb dispatch.

:class:`Server` is the **base class** every protocol surface builds on: it holds
one assembled kernel (config + api + truth-source) and exposes :meth:`dispatch`,
the verb router that the CLI and HTTP/MCP surfaces all share. A concrete surface
subclasses it and adds its transport (see
:class:`jiuwen_memory_entry.http_server.__main__.HttpServer` for the HTTP/socket surface);
the CLI's ``InProcessClient`` uses the base directly.

The minimal reference build uses :func:`api.build_kernel` (the per-capability
impls wired together, pure in-memory, no external deps). Swapping in a real profile
means assembling real plugins/Stores in :meth:`build` and reusing the same
``dispatch``.

本模块仍按 flat import root 使用（``import server`` / ``import profiles``）。
内核依赖改为 ``jiuwen_memory.*``；本地脚本把仓库根与 ``jiuwen_memory_entry/core`` 放入
``PYTHONPATH``，这里仅在直接运行时把仓库根追加为兜底路径。
"""

from __future__ import annotations

import logging
import os
import sys
from importlib import import_module
from typing import Any

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    # 直接运行且未做 editable 安装/PYTHONPATH 配置时的兜底。导入优先级由 Docker
    # editable 安装或 scripts/run-*.sh 保证，避免运行时把路径强插到最前。
    sys.path.append(_REPO)

Config = import_module("profiles").Config

_api_module = import_module("jiuwen_memory.api")
Kernel = _api_module.Kernel
build_kernel = _api_module.build_kernel
KernelConfig = import_module("jiuwen_memory.config").Config
Factory = import_module("jiuwen_memory.common.factory.factory").Factory
_security_runtime = import_module("jiuwen_memory.common.security.runtime")
SecurityRuntimeProducer = _security_runtime.SecurityRuntimeProducer
register_plugins = import_module("jiuwen_memory.common.bootstrap").register_plugins
ValidationError = import_module("jiuwen_memory.common.errors").ValidationError

_LOG = logging.getLogger(__name__)


def _apply_dev_permission_fallback(memory_api: Any) -> Any:
    """把 DEV 兼容的恒放行权限注入到 ``memory_api`` 配置的副本（不修改用户原字典）。

    触发条件 = DEV 回落 且 用户未显式选择 permission：
    - DEV 回落：memory_api 里没有 ``security`` 段，``build_security_runtime`` 会回落
      DevAuthenticator + loopback BindingPolicy（F05 的显式、可切换 DEV 模式）；
    - 未显式选 permission：memory_api 里没有 ``permission`` 段，默认上下文预置的
      sqlite/:memory: 只是内置回退，不算用户选择。

    两者其一被显式配置，都尊重用户选择，不注入。注入的是 ``permission.default=allow_all``：
    保留默认上下文里那个 sqlite 实例不动，但把 ``default`` 换掉——PR1 的权限门只收 Scope、
    不消费 role，role 的正式跨组织授权语义留给 PR2 Authorizer；DEV 模式没有远端攻击面、
    也没有配置安全边界的义务，这里用恒放行的 AllowAll 保住既有本地业务流程不因「未接 PR2」
    而断流。公共入口 :func:`api.build_kernel` 不做此覆写，默认权限仍是 sqlite。

    "当前是否显式声明了某段"只看配置原文，不看合并后的默认上下文——默认上下文预置
    permission/sqlite 不构成用户选择，否则 DEV 回落的判断会被自己把自己顶掉。
    """
    if memory_api is None:
        memory_api = {}
    probe = KernelConfig.from_dict(memory_api).context(known_top_names=Factory.known_top_names())
    namespaces = probe.namespaces
    if namespaces.get("security") or namespaces.get("permission"):
        return memory_api
    _LOG.warning(
        "未配置 permission 段且未配置 security 段：DEV 兼容回退，权限默认注入 "
        "permission.default=allow_all（仅限 loopback 的全放行，不得用于生产）。"
        "生产部署须显式配置 security.default 并把 authenticator 指向 api_key 或 trusted，"
        "同时显式选择 permission 实现。"
    )
    injected = dict(memory_api)
    permission = dict(injected.get("permission") or {})
    permission.setdefault("default", {"target": "allow_all"})
    injected["permission"] = permission
    return injected


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
    def ingest_jobs(self):
        return self.kernel.ingest_jobs

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

        在交给 ``build_kernel`` 之前，先经 :func:`_apply_dev_permission_fallback` 把 DEV 兼容的
        恒放行权限注入到该段的副本上：只有这条 bootstrap 装配路径能同时确认「无 security 段
        确会回落 DEV」「DEV 强制 loopback 绑定」「用户未显式选 permission」，所以 DEV 兼容
        覆写只在这里发生，公共 :func:`api.build_kernel` / :func:`api.assemble` 的默认权限保持
        sqlite。注入的对象是副本，不改 ``config.settings`` 原字典。
        """
        # 必须在 from_dict 之前：security / authenticator / key_store 等顶层段名要先进
        # Factory.known_top_names()，否则配置解析期会把它们当未知段拒掉。
        register_plugins()
        memory_api = _apply_dev_permission_fallback(config.settings.get("memory_api"))
        kernel_config = KernelConfig.from_dict(memory_api)
        kernel = build_kernel(policies=config.policies or None, config=kernel_config)
        security = build_security_runtime(kernel_config)
        return cls(config, kernel, security)

    def dispatch(self, verb: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Route a ``(verb, payload)`` through the shared handler."""
        from handler import dispatch as _dispatch

        return _dispatch(self, verb, payload)

    def close(self, *, wait: bool = True) -> None:
        """Release the Control-owned ingest worker pool."""
        self.ingest_jobs.close(wait=wait)


def build_security_runtime(kernel_config: Any):
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
            "未配置 security 段，回落 DEV 模式：请求以 system/dev（role=ROOT）鉴权，"
            "绑定仅限 loopback，不可暴露到远端。生产部署须显式配置 security.default "
            "并把 authenticator 指向 api_key 或 trusted。"
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


def default_spaces() -> dict[str, Any]:
    """Default scope/namespace registry (none needed for the in-memory build)."""
    return {}


def build(config: Config, spaces: Any = None) -> Server:
    """Module-level assembly shim (the CLI's ``InProcessClient`` calls this)."""
    return Server.build(config, spaces)
