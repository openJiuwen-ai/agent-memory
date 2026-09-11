# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Access 层可用的安全能力装配辅助。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwen_memory.common.security.authentication.base import Authenticator
from jiuwen_memory.common.security.authentication_impl import DevAuthenticator


def build_dev_authenticator(*, identities: Mapping[str, Any] | None = None) -> Authenticator:
    """构造固定身份或服务端预设身份认证器，仅供 HTTP / CLI 开发测试。"""
    return DevAuthenticator(identities=identities)
