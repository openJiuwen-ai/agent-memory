# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""loopback 绑定策略：要求 loopback 的认证能力只允许绑定 localhost。

判断依据是认证能力自报的 ``requires_loopback_binding()``，不是 target 名——
第三方认证实现只要声明自己具备远程暴露所需的保护，无需改本模块即可放行。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Sequence

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.protection.binding_policy import (
    BindingPolicy,
    BindingPolicyProducer,
)

_LOG = logging.getLogger(__name__)

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
# 「绑定所有网卡」的各种写法。空串在 socket 语义里等价于 0.0.0.0——
# 这是容器化场景下最危险的情况：以为只是没配，实际暴露给了整个网络。
_WILDCARD = frozenset({"0.0.0.0", "::", "*", ""})


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


class LoopbackBindingPolicy(BindingPolicy):
    """要求 loopback 的认证能力只允许绑定 localhost。"""

    def check(self, hosts: str | Sequence[str] | None, *, requires_loopback: bool) -> None:
        if not requires_loopback:
            return

        if hosts is None:
            candidates: list[str] = [""]
        elif isinstance(hosts, str):
            candidates = [hosts]
        else:
            candidates = [str(h) for h in hosts] or [""]

        # 多网卡时**任一** host 危险即拒绝。
        for host in candidates:
            normalized = host.strip().strip("[]").lower()
            if normalized in _WILDCARD:
                raise ValidationError(
                    f"当前认证能力要求 loopback 绑定，禁止绑定 {host!r}（等价于所有网卡）："
                    "该能力未声明具备远程暴露所需的认证保护，绑到非 localhost 等于把全权限"
                    "暴露给整个网络。请改绑 127.0.0.1，或配置 authenticator.default.target "
                    "为 api_key / trusted。"
                )
            if normalized not in _LOOPBACK:
                raise ValidationError(
                    f"当前认证能力要求 loopback 绑定，只允许绑定 localhost，得到 {host!r}。"
                    "请改绑 127.0.0.1，或配置 authenticator.default.target 为 api_key / trusted。"
                )

        if _in_container():
            # 容器里绑 127.0.0.1 本身是合法的，是否真的暴露取决于 port mapping /
            # Service，框架无法检查——故只警告不拒绝。
            _LOG.warning(
                "检测到容器环境且认证能力要求 loopback：即使绑定 127.0.0.1，是否对外暴露仍"
                "取决于 port mapping / Service 配置，框架无法检查。生产部署请使用 api_key "
                "或 trusted 认证。"
            )

    def health(self) -> None:
        return None


@BindingPolicyProducer.register("loopback")
def _build(config):
    return LoopbackBindingPolicy()
