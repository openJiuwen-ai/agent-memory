# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""开发认证器的固定身份与安全能力契约。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.security.authentication_impl import DevAuthenticator
from jiuwen_memory.common.security.types import Credentials, Role
from jiuwen_memory.common.type_def import Scope

pytestmark = pytest.mark.unit


def test_dev_authenticator_ignores_credentials_and_returns_named_root() -> None:
    authenticator = DevAuthenticator()

    missing = authenticator.authenticate(Credentials())
    supplied = authenticator.authenticate(Credentials(api_key="ignored"))

    assert missing == supplied
    assert missing.actor == Scope(org="local", user="developer")
    assert missing.role is Role.ROOT
    assert missing.credential_type == "dev"
    assert missing.auth_method == "dev"
    assert missing.actor != Scope()


def test_dev_authenticator_declares_local_lightweight_capabilities() -> None:
    authenticator = DevAuthenticator()

    assert authenticator.mode() == "dev"
    assert authenticator.requires_loopback_binding() is True
    assert authenticator.requires_concurrency_guard() is False
    assert authenticator.health() is None
