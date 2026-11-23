# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared Markdown wrapper handling for model JSON responses."""

import json
from typing import Any


def strip_json_fence(response: str) -> str:
    text = response.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")[1:]
    if lines and lines[-1].strip() == "```":
        lines.pop()
    return "\n".join(lines).strip()


def parse_json_response(response: str) -> Any:
    if not isinstance(response, str):
        raise TypeError("model response must be a string")
    return json.loads(strip_json_fence(response))
