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

本模块仍按 flat import root 使用（``import server`` / ``import profiles``）。
内核依赖改为 ``jiuwen_memory.*``；本地脚本把仓库根与 ``bootstrap/core`` 放入
``PYTHONPATH``，这里仅在直接运行时把仓库根追加为兜底路径。
"""

from __future__ import annotations

import os
import sys
from importlib import import_module
from typing import Any, Dict, Tuple

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


class Server:
    """Assembled kernel + shared dispatch; base for all protocol surfaces."""

    def __init__(self, config: Config, kernel: Kernel) -> None:
        """初始化 Server。

        Args:
            config: 参数 config（Config）。
            kernel: 参数 kernel（Kernel）。
        """
        self.config = config
        self.kernel = kernel
        self.ingest_jobs = kernel.ingest_jobs

    @property
    def api(self):
        """返回 api 属性。"""
        return self.kernel.api

    @property
    def kv(self):
        """返回 kv 属性。"""
        return self.kernel.kv

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
        kernel_config = KernelConfig.from_dict(config.settings.get("memory_api"))
        return cls(
            config,
            build_kernel(policies=config.policies or None, config=kernel_config),
        )

    def dispatch(self, verb: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Route a ``(verb, payload)`` through the shared handler."""
        from handler import dispatch as _dispatch

        return _dispatch(self, verb, payload)

    def close(self, *, wait: bool = True) -> None:
        """Release the Control-owned ingest worker pool."""
        self.ingest_jobs.close(wait=wait)


def default_spaces() -> Dict[str, Any]:
    """Default scope/namespace registry (none needed for the in-memory build)."""
    return {}


def build(config: Config, spaces: Any = None) -> Server:
    """Module-level assembly shim (the CLI's ``InProcessClient`` calls this)."""
    return Server.build(config, spaces)
