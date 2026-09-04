# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Access 层可用的安全能力装配辅助。"""

from __future__ import annotations

from jiuwen_memory.common.security.authentication.base import Authenticator
from jiuwen_memory.common.security.authentication_impl import DevAuthenticator


def build_dev_authenticator() -> Authenticator:
    """构造仅供本地 HTTP / CLI 功能测试使用的固定身份认证器。"""
    return DevAuthenticator()
