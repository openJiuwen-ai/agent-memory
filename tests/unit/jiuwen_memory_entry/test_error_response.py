# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared mapping preserves rate limits and redacts provider details."""

import pytest

from jiuwen_memory.api import RateLimitedError
from jiuwen_memory_entry.core.error_response import error_response
from tests.unit.jiuwen_memory_entry.fixtures import upstream_api_error

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("kind", ["sdk", "domain"])
@pytest.mark.parametrize("form", ["instance", "class", "name"])
def test_rate_limit_response(kind: str, form: str) -> None:
    error = upstream_api_error() if kind == "sdk" else RateLimitedError("private-detail")
    value = {"instance": error, "class": type(error), "name": type(error).__name__}[form]
    assert error_response(value, error) == (
        429,
        {"error": type(error).__name__, "message": "too many requests", "retryable": True},
        1,
    )


@pytest.mark.parametrize("status", [400, 500])
def test_other_sdk_errors_remain_internal(status: int) -> None:
    error = upstream_api_error(status)
    assert error_response(error, error) == (
        500,
        {"error": "InternalError", "message": "internal server error", "retryable": False},
        None,
    )
