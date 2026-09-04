# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared JSON contract and direct invocation for HTTP and CLI MemoryAPI calls.

HTTP and CLI expose every public ``MemoryAPI`` method under the same name.
This module derives accepted fields, defaults and Python target types from the
``MemoryAPI`` signature so the transport cannot silently grow a second API.
Authenticated ``security`` is the sole parameter that never comes from JSON.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import types
import typing
from collections.abc import Mapping, Sequence, Set
from datetime import datetime
from enum import Enum
from functools import lru_cache
from typing import Any, Awaitable, get_args, get_origin, get_type_hints

from jiuwen_memory.api import MemoryAPI, ValidationError

_RESERVED_SECURITY_FIELDS = {
    "security",
    "identity",
    "actor",
    "acting_user",
    "principal",
    "authenticated_user",
}


@dataclasses.dataclass(frozen=True)
class MethodContract:
    """One transport method contract derived from ``MemoryAPI``."""

    name: str
    signature: inspect.Signature
    type_hints: Mapping[str, Any]
    is_async: bool

    @property
    def request_parameters(self) -> Mapping[str, inspect.Parameter]:
        """Return JSON-supplied parameters, excluding ``self`` and ``security``."""
        return {
            name: parameter
            for name, parameter in self.signature.parameters.items()
            if name not in {"self", "security"}
        }


def api_method_names() -> frozenset[str]:
    """Return the exact public abstract method set exposed by HTTP and CLI."""
    return frozenset(MemoryAPI.__abstractmethods__)


def is_known_verb(verb: str) -> bool:
    """Return whether ``verb`` names a public ``MemoryAPI`` method."""
    return verb in api_method_names()


@lru_cache(maxsize=None)
def method_contract(verb: str) -> MethodContract:
    """Build the transport contract for one public ``MemoryAPI`` method."""
    if not is_known_verb(verb):
        raise ValidationError(f"unknown MemoryAPI method: {verb!r}")
    method = getattr(MemoryAPI, verb)
    return MethodContract(
        name=verb,
        signature=inspect.signature(method),
        type_hints=get_type_hints(method),
        is_async=inspect.iscoroutinefunction(method),
    )


def parse_request(verb: str, raw: Any) -> dict[str, Any]:
    """Validate and decode one JSON object into same-named ``MemoryAPI`` arguments."""
    if not isinstance(raw, dict):
        raise ValidationError("request body must be a JSON object")
    contract = method_contract(verb)
    parameters = contract.request_parameters

    for name in raw:
        if name in _RESERVED_SECURITY_FIELDS or name.startswith("actor_"):
            raise ValidationError(f"field {name!r} is supplied by authentication")
        if name not in parameters:
            raise ValidationError(f"unknown field for MemoryAPI.{verb}: {name!r}")

    missing = [
        name
        for name, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty and name not in raw
    ]
    if missing:
        joined = ", ".join(repr(name) for name in missing)
        raise ValidationError(f"missing required field(s) for MemoryAPI.{verb}: {joined}")

    decoded: dict[str, Any] = {}
    for name, value in raw.items():
        annotation = contract.type_hints.get(name, Any)
        decoded[name] = _decode(value, annotation, path=name)
    return decoded


async def _await_api_result(result: Awaitable[Any]) -> Any:
    return await result


def invoke_api(api: Any, verb: str, payload: Any, security: Any) -> Any:
    """Call the same-named MemoryAPI method and serialize its original result."""
    arguments = parse_request(verb, payload)
    result = getattr(api, verb)(**arguments, security=security)
    if method_contract(verb).is_async:
        if not inspect.isawaitable(result):
            raise TypeError(f"MemoryAPI.{verb} did not return an awaitable")
        result = asyncio.run(_await_api_result(result))
    return to_jsonable(result)


def to_jsonable(value: Any) -> Any:
    """Convert a ``MemoryAPI`` return value to its mechanical JSON representation."""
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if not field.name.startswith("_")
        }
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        converted = [to_jsonable(item) for item in value]
        return sorted(converted, key=repr)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_jsonable(item) for item in value]
    raise TypeError(f"unsupported MemoryAPI return type: {type(value).__name__}")


def _decode(value: Any, annotation: Any, *, path: str) -> Any:
    if annotation is Any or annotation is inspect.Parameter.empty:
        return value

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in {types.UnionType, typing.Union}:
        if value is None and type(None) in args:
            return None
        errors: list[str] = []
        for candidate in args:
            if candidate is type(None):
                continue
            try:
                return _decode(value, candidate, path=path)
            except ValidationError as exc:
                errors.append(str(exc))
        detail = errors[-1] if errors else "no compatible type"
        raise ValidationError(f"{path} does not match the API type: {detail}")

    if value is None:
        raise ValidationError(f"{path} must not be null")
    if origin is list:
        if not isinstance(value, list):
            raise ValidationError(f"{path} must be an array")
        item_type = args[0] if args else Any
        return [
            _decode(item, item_type, path=f"{path}[{index}]") for index, item in enumerate(value)
        ]

    if origin is tuple:
        if not isinstance(value, list):
            raise ValidationError(f"{path} must be an array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(
                _decode(item, args[0], path=f"{path}[{index}]") for index, item in enumerate(value)
            )
        if args and len(value) != len(args):
            raise ValidationError(f"{path} must contain {len(args)} items")
        return tuple(
            _decode(item, args[index] if args else Any, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )

    if origin in {set, frozenset, Set}:
        if not isinstance(value, list):
            raise ValidationError(f"{path} must be an array")
        item_type = args[0] if args else Any
        decoded = {
            _decode(item, item_type, path=f"{path}[{index}]") for index, item in enumerate(value)
        }
        return frozenset(decoded) if origin is frozenset else decoded

    if origin in {dict, Mapping}:
        if not isinstance(value, dict):
            raise ValidationError(f"{path} must be an object")
        key_type, value_type = args if len(args) == 2 else (Any, Any)
        return {
            _decode(key, key_type, path=f"{path}.<key>"): _decode(
                item, value_type, path=f"{path}.{key}"
            )
            for key, item in value.items()
        }

    if inspect.isclass(annotation) and issubclass(annotation, Enum):
        try:
            return annotation(value)
        except (TypeError, ValueError):
            allowed = ", ".join(repr(item.value) for item in annotation)
            raise ValidationError(f"{path} must be one of: {allowed}") from None

    if annotation is datetime:
        if not isinstance(value, str):
            raise ValidationError(f"{path} must be an ISO 8601 datetime string")
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise ValidationError(f"{path} must be an ISO 8601 datetime string") from None

    if inspect.isclass(annotation) and dataclasses.is_dataclass(annotation):
        return _decode_dataclass(value, annotation, path=path)

    if annotation is bool:
        if not isinstance(value, bool):
            raise ValidationError(f"{path} must be a boolean")
        return value
    if annotation is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"{path} must be an integer")
        return value
    if annotation is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValidationError(f"{path} must be a number")
        return float(value)
    if annotation is str:
        if not isinstance(value, str):
            raise ValidationError(f"{path} must be a string")
        return value

    if inspect.isclass(annotation) and isinstance(value, annotation):
        return value
    raise ValidationError(f"{path} has unsupported API type {annotation!r}")


def _decode_dataclass(value: Any, cls: type, *, path: str) -> Any:
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must be an object")

    fields = {field.name: field for field in dataclasses.fields(cls) if field.init}
    unknown = [name for name in value if name not in fields or name.startswith("_")]
    if unknown:
        joined = ", ".join(repr(name) for name in unknown)
        raise ValidationError(f"unknown field(s) for {path}: {joined}")

    missing: list[str] = []
    for name, field in fields.items():
        if name in value:
            continue
        if field.default is not dataclasses.MISSING:
            continue
        if field.default_factory is not dataclasses.MISSING:
            continue
        missing.append(name)
    if missing:
        joined = ", ".join(repr(name) for name in missing)
        raise ValidationError(f"missing required field(s) for {path}: {joined}")

    hints = get_type_hints(cls)
    kwargs = {
        name: _decode(item, hints.get(name, Any), path=f"{path}.{name}")
        for name, item in value.items()
    }
    try:
        return cls(**kwargs)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"invalid {path}: {exc}") from None
