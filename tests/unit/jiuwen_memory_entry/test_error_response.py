# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared error envelopes preserve upstream and domain rate-limit semantics."""

import httpx
import pytest
from openai import InternalServerError, RateLimitError

from jiuwen_memory.common.errors import RateLimitedError
from jiuwen_memory_entry.core.error_response import error_response

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("error_form", ["instance", "class", "name"])
@pytest.mark.parametrize("source", ["domain", "openai"])
def test_rate_limits_remain_retryable_and_redacted(source: str, error_form: str) -> None:
    detail = "api_key=secret-key https://user:secret-password@example.com"
    if source == "domain":
        error = RateLimitedError(detail)
    else:
        response = httpx.Response(429, request=httpx.Request("POST", "https://example.com"))
        error = RateLimitError(detail, response=response, body={"message": detail})
    error_name = type(error).__name__
    value = {"instance": error, "class": type(error), "name": error_name}[error_form]

    status, body, retry_after = error_response(value, detail)

    assert status == 429
    assert body == {"error": error_name, "message": "too many requests", "retryable": True}
    assert retry_after == 1


def test_unmapped_sdk_errors_keep_generic_non_retryable_response() -> None:
    response = httpx.Response(500, request=httpx.Request("POST", "https://example.com"))
    error = InternalServerError("api_key=secret-key", response=response, body=None)

    status, body, retry_after = error_response(error, error)

    assert status == 500
    assert body == {
        "error": "InternalError",
        "message": "internal server error",
        "retryable": False,
    }
    assert retry_after is None
