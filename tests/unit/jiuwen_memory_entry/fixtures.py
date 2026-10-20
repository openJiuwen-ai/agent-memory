# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deterministic upstream API failures without network access or credentials."""

import httpx
import openai


def upstream_api_error(status: int = 429) -> openai.APIStatusError:
    error_types = {
        429: openai.RateLimitError,
        400: openai.BadRequestError,
        500: openai.InternalServerError,
    }
    error_type = error_types.get(status)
    if error_type is None:
        raise ValueError(f"unsupported upstream status: {status}")
    return error_type(
        "provider-private-detail Authorization: Bearer secret-token api_key=secret-key",
        response=httpx.Response(
            status,
            request=httpx.Request("POST", "https://provider.invalid/v1/embeddings"),
        ),
        body={"error": {"message": "provider-private-detail"}},
    )
