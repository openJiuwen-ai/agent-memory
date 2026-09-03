# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Generic handler 的既有错误映射回归测试。"""

from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import RateLimitedError
from jiuwen_memory.common.security.types import AuthContext, Role, reset_current, set_current
from jiuwen_memory.common.type_def.scope import Scope
from jiuwen_memory_entry.core import handler

pytestmark = pytest.mark.unit


def test_rate_limited_error_preserves_legacy_400_mapping() -> None:
    """审计接口 PR 不应把既有 HTTP 限流响应从 400 隐式改为 429。"""

    class _Api:
        @staticmethod
        def audit(_filters, *, security, limit=100):
            del security, limit
            raise RateLimitedError("rate limit exceeded")

    srv = SimpleNamespace(api=_Api())

    # dispatch 的身份来自认证上下文（铁律 #1）：不设上下文时 handler fail-closed 抛 401，
    # 测不到本用例真正关心的错误映射，故先以具名 actor 进入。
    token = set_current(AuthContext(actor=Scope(org="acme", user="alice"), role=Role.USER))
    try:
        status, body = handler.dispatch(srv, "audit", {})
    finally:
        reset_current(token)

    assert status == 400
    assert body == {"error": "RateLimitedError", "message": "rate limit exceeded"}
