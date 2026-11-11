# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Strict plaintext admission for explicit encryption imports, never for normal reads."""

from __future__ import annotations

import json
import math

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.common.type_def.memory_codec import dumps, loads


class FormatUpgradeRequired(ValidationError):
    """A historical schema must be upgraded explicitly before encryption migration."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _matches(value, canonical) -> bool:
    """Reject coercions and unknown fields while permitting absent optional fields."""
    if isinstance(value, dict):
        return isinstance(canonical, dict) and all(
            key in canonical and _matches(item, canonical[key]) for key, item in value.items()
        )
    if isinstance(value, list):
        return (
            isinstance(canonical, list)
            and len(value) == len(canonical)
            and all(_matches(item, other) for item, other in zip(value, canonical))
        )
    if isinstance(value, bool) or isinstance(canonical, bool):
        return type(value) is type(canonical) and value == canonical
    if isinstance(value, (int, float)) and isinstance(canonical, (int, float)):
        return math.isfinite(value) and value == canonical
    return type(value) is type(canonical) and value == canonical


def validate_plaintext_memory(raw: bytes, *, max_bytes: int) -> MemoryUnit:
    """Validate current serialized MemoryUnit without repairing or rewriting its bytes."""
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > max_bytes:
            raise ValueError("invalid input size")
        if raw.startswith(b"ENC1"):
            raise ValueError("encrypted input is not plaintext")
        payload = json.loads(raw, object_pairs_hook=_object)
        if not isinstance(payload, dict) or type(payload.get("_v")) is not int:
            raise ValueError("missing codec version")
        if payload["_v"] < 4:
            raise FormatUpgradeRequired("memory format upgrade required")
        if payload["_v"] != 4:
            raise ValueError("unsupported codec version")
        if not isinstance(payload.get("id"), str) or not payload["id"].strip():
            raise ValueError("missing memory id")
        scope = payload.get("scope")
        if (
            not isinstance(scope, list)
            or len(scope) != 5
            or not all(isinstance(part, str) and "\x00" not in part for part in scope)
        ):
            raise ValueError("invalid scope")
        if not scope[0]:
            raise ValueError("missing organization")
        for name in ("source_ref", "supersedes"):
            if not isinstance(payload.get(name, ""), str):
                raise ValueError("invalid string field")
        for name in ("tags", "entities", "provenance"):
            values = payload.get(name, [])
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError("invalid string list")
        for name in ("system_metadata", "user_metadata"):
            metadata = payload.get(name, {})
            if not isinstance(metadata, dict):
                raise ValueError("invalid metadata")
            for item in metadata.values():
                if item is None or isinstance(item, (str, bool)):
                    continue
                if type(item) in (int, float) and math.isfinite(item):
                    continue
                if isinstance(item, list) and all(isinstance(part, str) for part in item):
                    continue
                raise ValueError("invalid metadata value")
        for vector in payload.get("vectors", []):
            if not isinstance(vector, dict) or not isinstance(vector.get("vector", []), list):
                raise ValueError("invalid vector")
            if not all(
                type(item) in (int, float) and math.isfinite(item)
                for item in vector.get("vector", [])
            ):
                raise ValueError("invalid vector value")
        if not isinstance(payload.get("segments"), list):
            raise ValueError("missing memory segments")
        for segment in payload["segments"]:
            if not isinstance(segment, dict) or not isinstance(segment.get("content"), str):
                raise ValueError("invalid memory segment")
            if not isinstance(segment.get("assets", []), list) or not all(
                isinstance(asset, str) for asset in segment.get("assets", [])
            ):
                raise ValueError("invalid asset references")
        unit = loads(raw)
        if unit is None or not _matches(payload, json.loads(dumps(unit))):
            raise ValueError("invalid memory fields")
        return unit
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise ValidationError("invalid plaintext memory format") from None
