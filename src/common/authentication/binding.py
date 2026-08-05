"""DEV 认证模式的绑定地址校验（security.md §2.2.1）。

DEV 模式恒返回 ROOT 身份，绑到非 localhost 就是把全权限暴露给整个网络。
本模块是纯函数、抛异常，**不 ``sys.exit``**：exit 语义留在真正的进程入口
（``bootstrap/http_server/__main__.py:main``），这样本函数可被单测直接断言，
而不会让测试进程退出。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Sequence

from common.errors import ValidationError

_LOG = logging.getLogger(__name__)

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
# 「绑定所有网卡」的各种写法。空串在 socket 语义里等价于 0.0.0.0——
# 这是容器化场景下最危险的情况：以为只是没配，实际暴露给了整个网络。
_WILDCARD = frozenset({"0.0.0.0", "::", "*", ""})


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def check_dev_binding(hosts: str | Sequence[str] | None) -> None:
    """DEV 模式的绑定地址校验：非 localhost 抛 :class:`~common.errors.ValidationError`。

    调用方负责把异常翻译成打印 + 退出码。多网卡时**任一** host 危险即拒绝。

    容器环境只打 WARNING 不拒绝：容器里绑 127.0.0.1 本身是合法的，是否真的
    暴露取决于 port mapping / Service，框架无法检查（§2.2.1 原话）。
    """
    if hosts is None:
        candidates: list[str] = [""]
    elif isinstance(hosts, str):
        candidates = [hosts]
    else:
        candidates = [str(h) for h in hosts] or [""]

    for host in candidates:
        normalized = host.strip().strip("[]").lower()
        if normalized in _WILDCARD:
            raise ValidationError(
                f"DEV 认证模式禁止绑定 {host!r}（等价于所有网卡）：该模式恒返回 ROOT 身份，"
                "绑到非 localhost 等于把全权限暴露给整个网络。"
                "请改绑 127.0.0.1，或配置 authenticator.default.target 为 api_key / trusted。"
            )
        if normalized not in _LOOPBACK:
            raise ValidationError(
                f"DEV 认证模式只允许绑定 localhost，得到 {host!r}。"
                "请改绑 127.0.0.1，或配置 authenticator.default.target 为 api_key / trusted。"
            )

    if _in_container():
        _LOG.warning(
            "检测到容器环境且认证模式为 DEV：即使绑定 127.0.0.1，是否对外暴露仍取决于 "
            "port mapping / Service 配置，框架无法检查。生产部署请使用 api_key 或 trusted 模式。"
        )
