# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Generic handler 的既有错误映射回归测试。"""

from types import SimpleNamespace

import pytest

from jiuwen_memory_entry.core import handler
from jiuwen_memory.common.errors import RateLimitedError

pytestmark = pytest.mark.unit


def test_rate_limited_error_preserves_legacy_400_mapping() -> None:
    """审计接口 PR 不应把既有 HTTP 限流响应从 400 隐式改为 429。"""

    class _Api:
        @staticmethod
        def audit(_filters, *, security, limit=100):
            del security, limit
            raise RateLimitedError("rate limit exceeded")

    srv = SimpleNamespace(api=_Api())

    status, body = handler.dispatch(srv, "audit", {})

    assert status == 400
    assert body == {"error": "RateLimitedError", "message": "rate limit exceeded"}
