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
from typing import Any

from profiles import Config

from jiuwen_memory.api import (
    MemoryRuntime,
    RequestSecurityContext,
    assemble_runtime,
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
        # composition root 必须调和私有 Runtime/PEP 真源；不扩充冻结的公共句柄。
        # pylint: disable=protected-access
        runtime = assemble_runtime(policies=config.policies or None, config=memory_api)
        auto_security = security_runtime is _AUTO_SECURITY
        if auto_security:
            # 内核装配先重置 Factory 缓存并构建存储依赖；安全 Runtime 随后从同一组
            # 具名缓存取依赖，确保 cryptography 等有状态组件不会被构建成第二份。
            security_runtime = runtime._security_runtime
            # AUTO 路径校验 identity：这里 Runtime 与内核 PEP 由同一份配置在同一
            # 装配纪元（reset 之后）构建，分叉只可能是配置让 Runtime 指到了别的具名
            # authorizer——那是启动期必须拒绝的配置错误（不变量 30）。
            # 显式注入路径来自另一装配纪元，不能拿 identity 比较；它在下方改为把 Runtime
            # 的实例绑定给 PEP，同样收敛成一个对象。
            runtime_authorizer = getattr(security_runtime, "authorizer", None)
            if runtime_authorizer is not None and runtime_authorizer is not runtime.api._authorizer:
                from jiuwen_memory.api import ValidationError

                raise ValidationError(
                    "security 段的 authorizer 必须与内核 PEP 是同一实例；"
                    "当前配置使 Runtime 与 PEP 的 Grant/DelegationStore 视图分叉"
                )
        if security_runtime is not None:
            runtime_authorizer = getattr(security_runtime, "authorizer", None)
            if runtime_authorizer is not None and runtime_authorizer.is_test_only():
                from jiuwen_memory.api import ValidationError

                raise ValidationError(
                    "Server refuses a test-only authorizer; configure a production PDP"
                )
            if not auto_security and runtime_authorizer is not None:
                # 显式 Runtime 也必须与 PEP 共用同一个 PDP。先装内核、再绑定调用方已装配
                # 的实例，避免两个装配纪元各持一份 Grant/DelegationStore（P2-2）。
                runtime.api._bind_authorizer(runtime_authorizer)
            # Registry 调和两条路径都做（P1-1/P2-2）：凭据撤销复核的真源只能来自
            # 实际装配的 Authenticator，与配置写法（内联/具名）无关。显式注入路径也
            # 完成 PDP 与凭据真源绑定——
            # Registry 注册的必须是认证器签发用的那一个 Store，否则复核读的是
            # 另一份事实，撤销在认证侧生效、在 PEP 侧看不见。
            authenticator = getattr(security_runtime, "authenticator", None)
            if authenticator is not None:
                runtime.api._bind_credential_sources(authenticator)
        return cls(config, runtime, security_runtime)

    def dispatch(
        self,
        verb: str | DispatchRequest,
        payload: dict[str, Any] | None = None,
        *,
        security: RequestSecurityContext,
    ) -> tuple[int, dict[str, Any]]:
        """Route a legacy request through the shared handler.

        ``security`` is an adapter-produced trusted context, **required**. MCP uses
        this path; HTTP and CLI call ``api`` directly. Callers without a trusted
        identity source must not use this path—route through HTTP/API-Key instead.
        """
        from handler import dispatch as _dispatch

        if isinstance(verb, DispatchRequest):
            status, body = _dispatch(self, verb)
            return status, dict(body)
        if security is None:
            from jiuwen_memory.api import ValidationError

            raise ValidationError("dispatch requires an explicit trusted security context")
        request = build_legacy_dispatch_request(verb, payload or {}, security=security)
        status, body = _dispatch(self, request)
        return status, dict(body)

    def close(self, *, wait: bool = True) -> None:
        """Release the Control-owned ingest worker pool."""
        self._runtime.close(wait=wait)
        if self.security_runtime is not None and self.security_runtime is not getattr(
            self._runtime, "_security_runtime", None
        ):
            closer = getattr(self.security_runtime, "close", None)
            if callable(closer):
                closer()


def default_spaces() -> dict[str, Any]:
    """Default scope/namespace registry (none needed for the in-memory build)."""
    return {}


def build(config: Config, spaces: Any = None) -> Server:
    """Module-level assembly shim for callers using the flat import root."""
    return Server.build(config, spaces)
