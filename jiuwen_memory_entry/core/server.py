# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Base surface-server — kernel assembly + the shared verb dispatch.

:class:`Server` is the **base class** every protocol surface builds on: it holds
one assembled runtime (config + api + ingest lifecycle) and exposes :meth:`dispatch`,
the verb router that the CLI and HTTP/MCP surfaces all share. A concrete surface
subclasses it and adds its transport (see
:class:`jiuwen_memory_entry.http_server.__main__.HttpServer` for the HTTP/socket
surface); the CLI's ``InProcessClient`` uses the base directly.

The minimal reference build uses :func:`api.assemble_runtime` (the per-capability
impls wired together, pure in-memory, no external deps). Swapping in a real profile
means assembling real plugins/Stores in :meth:`build` and reusing the same
``dispatch``.

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

from jiuwen_memory.api import MemoryRuntime, Surface, assemble_runtime
from jiuwen_memory_entry.core.dispatch_request import DispatchRequest
from jiuwen_memory_entry.core.legacy_request_adapter import build_legacy_dispatch_request

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    # 直接运行且未做 editable 安装/PYTHONPATH 配置时的兜底。导入优先级由 Docker
    # editable 安装或 scripts/run-*.sh 保证，避免运行时把路径强插到最前。
    sys.path.append(_REPO)


class Server:
    """Assembled runtime + shared dispatch; base for all protocol surfaces."""

    def __init__(self, config: Config, runtime: MemoryRuntime) -> None:
        self.config = config
        self._runtime = runtime

    @property
    def api(self):
        return self._runtime.api

    async def start_background(self) -> None:
        """异步面宿主调用：起看门狗等后台组件（绑当前 loop，F07 §12.10）。"""
        await self._runtime.start_background()

    def start(self) -> None:
        """同步面宿主调用：daemon 线程自持 loop 起后台组件（F07 §12.10）。"""
        self._runtime.start()

    @classmethod
    def build(cls, config: Config, spaces: Any = None) -> "Server":
        """Assemble a runtime from ``config`` and return a ``cls`` instance.

        ``config.settings`` 是合并后的完整配置字典，含 profiles 层自有的 ``profile`` /
        ``policies`` 等顶层键；其中 ``memory_api`` 段（若有）才是交给内核的**两级命名空间**
        装配配置，由 :func:`api.assemble_runtime` 合并覆盖到内置默认之上。须**只取该段**
        交装配 —— 整包传入会让 ``profile`` / ``policies`` 撞上新配置解析期的顶层段名
        校验而报错。无该段时（纯 ``OFFLINE`` 档）``config=None`` 回落进程内默认实现。
        """
        memory_api = config.settings.get("memory_api")
        return cls(
            config,
            assemble_runtime(policies=config.policies or None, config=memory_api),
        )

    def dispatch(
        self,
        verb: str | DispatchRequest,
        payload: dict[str, Any] | None = None,
        *,
        identity=None,
    ) -> tuple[int, dict[str, Any]]:
        """Route a request through the shared handler.

        ``identity`` is an adapter-supplied actor.  HTTP passes the actor from
        its authenticated request context; CLI/MCP omit it and retain their
        existing in-process compatibility path.
        """
        from handler import dispatch as _dispatch

        if isinstance(verb, DispatchRequest):
            status, body = _dispatch(self, verb)
            return status, dict(body)
        request = build_legacy_dispatch_request(verb, payload or {}, surface=Surface.INTERNAL)
        if identity is not None:
            request = replace(request, actor=identity)
        status, body = _dispatch(self, request)
        return status, dict(body)

    def close(self, *, wait: bool = True) -> None:
        """Release the Control-owned ingest worker pool."""
        self._runtime.close(wait=wait)


def default_spaces() -> dict[str, Any]:
    """Default scope/namespace registry (none needed for the in-memory build)."""
    return {}


def build(config: Config, spaces: Any = None) -> Server:
    """Module-level assembly shim (the CLI's ``InProcessClient`` calls this)."""
    return Server.build(config, spaces)
