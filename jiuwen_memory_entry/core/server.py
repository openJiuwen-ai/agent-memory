# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Base surface-server — kernel assembly + the legacy verb dispatch.

:class:`Server` is the **base class** every protocol surface builds on: it holds
one assembled runtime (config + api + ingest lifecycle) and exposes :attr:`api`
plus :meth:`dispatch`. MCP and historical in-process callers use the legacy
verb router; HTTP and CLI call the same-named ``MemoryAPI`` method directly. A concrete
surface subclasses this class and adds its transport (see
:class:`jiuwen_memory_entry.http_server.__main__.HttpServer` for the HTTP/socket
surface); the CLI's ``InProcessClient`` uses the base directly.

The minimal reference build uses :func:`api.assemble_runtime` (the per-capability
impls wired together, pure in-memory, no external deps). Swapping in a real profile
means assembling real plugins/Stores in :meth:`build` and reusing the same API.

本模块是 Access 的 **composition root**：只通过 ``jiuwen_memory.api.assemble_runtime``
装配内核（传入 dict，不 import ``jiuwen_memory.config``）。公开面只保留 ``api``、
``dispatch()`` 和 surface lifecycle，不暴露 raw KV。

本模块仍按 flat import root 使用（``import server`` / ``import profiles``）。
内核依赖改为 ``jiuwen_memory.api``；本地脚本把仓库根与 ``jiuwen_memory_entry/core`` 放入
``PYTHONPATH``，这里仅在直接运行时把仓库根追加为兜底路径。
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from typing import Any

from profiles import Config

from jiuwen_memory.api import (
    MemoryRuntime,
    Surface,
    assemble_runtime,
    build_configured_security_runtime,
)
from jiuwen_memory_entry.core.dispatch_request import DispatchRequest
from jiuwen_memory_entry.core.legacy_request_adapter import build_legacy_dispatch_request

_AUTO_SECURITY = object()

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    # 直接运行且未做 editable 安装/PYTHONPATH 配置时的兜底。导入优先级由 Docker
    # editable 安装或 scripts/run-*.sh 保证，避免运行时把路径强插到最前。
    sys.path.append(_REPO)


class Server:
    """Assembled runtime + shared dispatch; base for all protocol surfaces."""

    def __init__(
        self, config: Config, runtime: MemoryRuntime, security_runtime: Any = None
    ) -> None:
        self.config = config
        self._runtime = runtime
        self.security_runtime = security_runtime

    @property
    def api(self):
        return self._runtime.api

    @property
    def audit(self):
        """返回供认证边界记录入口事件的共享审计器。"""
        return getattr(self._runtime, "_audit", None)

    @property
    def authenticator(self):
        return getattr(self.security_runtime, "authenticator", None)

    @property
    def rate_limiter(self):
        return getattr(self.security_runtime, "rate_limiter", None)

    @property
    def workload_guard(self):
        guard = getattr(self.security_runtime, "workload_guard", None)
        authenticator = self.authenticator
        if authenticator is None or not authenticator.requires_concurrency_guard():
            return None
        return guard

    @property
    def binding_policy(self):
        return getattr(self.security_runtime, "binding_policy", None)

    @classmethod
    def build(
        cls,
        config: Config,
        spaces: Any = None,
        *,
        security_runtime: Any = _AUTO_SECURITY,
    ) -> "Server":
        """Assemble a runtime from ``config`` and return a ``cls`` instance.

        ``config.settings`` 是合并后的完整配置字典，含 profiles 层自有的 ``profile`` /
        ``policies`` 等顶层键；其中 ``memory_api`` 段（若有）才是交给内核的**两级命名空间**
        装配配置，由 :func:`api.assemble_runtime` 合并覆盖到内置默认之上。须**只取该段**
        交装配 —— 整包传入会让 ``profile`` / ``policies`` 撞上新配置解析期的顶层段名
        校验而报错。无该段时（纯 ``OFFLINE`` 档）``config=None`` 回落进程内默认实现。
        """
        memory_api = config.settings.get("memory_api")
        runtime = assemble_runtime(policies=config.policies or None, config=memory_api)
        if security_runtime is _AUTO_SECURITY:
            # 内核装配先重置 Factory 缓存并构建存储依赖；安全 Runtime 随后从同一组
            # 具名缓存取依赖，确保 cryptography 等有状态组件不会被构建成第二份。
            security_runtime = build_configured_security_runtime(memory_api)
        return cls(config, runtime, security_runtime)

    def dispatch(
        self,
        verb: str | DispatchRequest,
        payload: dict[str, Any] | None = None,
        *,
        identity=None,
        security=None,
    ) -> tuple[int, dict[str, Any]]:
        """Route a legacy request through the shared handler.

        ``security`` is an adapter-produced trusted context and overrides every legacy
        actor field. MCP uses this path; HTTP and CLI call ``api`` directly. ``identity``
        remains only for historical in-process adapters until PR2 completes the migration.
        """
        from handler import dispatch as _dispatch

        if isinstance(verb, DispatchRequest):
            status, body = _dispatch(self, verb)
            return status, dict(body)
        request = build_legacy_dispatch_request(verb, payload or {}, surface=Surface.INTERNAL)
        if security is not None:
            request = replace(
                request,
                actor=security.actor,
                surface=security.surface,
                security=security,
            )
        elif identity is not None:
            request = replace(request, actor=identity)
        status, body = _dispatch(self, request)
        return status, dict(body)

    def close(self, *, wait: bool = True) -> None:
        """Release the Control-owned ingest worker pool."""
        self._runtime.close(wait=wait)
        if self.security_runtime is not None:
            closer = getattr(self.security_runtime, "close", None)
            if callable(closer):
                closer()


def default_spaces() -> dict[str, Any]:
    """Default scope/namespace registry (none needed for the in-memory build)."""
    return {}


def build(config: Config, spaces: Any = None) -> Server:
    """Module-level assembly shim for callers using the flat import root."""
    return Server.build(config, spaces)
