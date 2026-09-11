# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP 开发认证模式的最小运行时适配。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jiuwen_memory.api import build_dev_authenticator


@dataclass(frozen=True)
class DevHttpSecurityRuntime:
    """HTTP 中间件所需的最小开发运行时，不冒充完整 SecurityRuntime。"""

    authenticator: Any
    rate_limiter: None = None
    workload_guard: None = None
    audit: None = None


def build_dev_security_runtime(
    *, identities: Mapping[str, Any] | None = None
) -> DevHttpSecurityRuntime:
    """构造固定身份或预设身份映射的 HTTP 开发运行时，不含生产保护组件。"""
    return DevHttpSecurityRuntime(authenticator=build_dev_authenticator(identities=identities))
