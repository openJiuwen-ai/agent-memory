# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Generate CLI commands from MemoryAPI and render its original JSON results."""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from datetime import datetime
from enum import Enum
from functools import partial
from typing import Any, get_args

from jiuwen_memory.api import ValidationError
from jiuwen_memory_entry.core.api_contract import api_method_names, method_contract, parse_request


class CliError(Exception):
    """Invalid command-line input, reported without a traceback."""


def add_output_args(parser) -> None:
    """Add display-only options that never enter API arguments."""
    parser.add_argument("--output", choices=("json", "text", "table", "quiet"), default="json")
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")


def _argument_value(raw: str, *, annotation: Any) -> Any:
    alternatives = get_args(annotation)
    if type(None) in alternatives and raw == "null":
        return None
    candidates = alternatives or (annotation,)
    for candidate in candidates:
        if candidate in (str, datetime):
            return raw
        if inspect.isclass(candidate) and issubclass(candidate, Enum):
            return raw
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a JSON value") from exc


def add_api_arguments(parser, verb: str) -> None:
    """Expose every non-security API parameter with its exact name."""
    contract = method_contract(verb)
    for name, parameter in contract.request_parameters.items():
        parser.add_argument(
            f"--{name}",
            dest=f"api_{name}",
            type=partial(_argument_value, annotation=contract.type_hints.get(name, Any)),
            required=parameter.default is inspect.Parameter.empty,
            default=argparse.SUPPRESS,
            help=f"MemoryAPI.{verb}.{name}; objects and collections use JSON",
        )
    add_output_args(parser)


def build_payload(verb: str, args) -> dict[str, Any]:
    """Read supplied API options without filling or changing API defaults."""
    payload = {
        name: getattr(args, f"api_{name}")
        for name in method_contract(verb).request_parameters
        if hasattr(args, f"api_{name}")
    }
    try:
        parse_request(verb, payload)
    except ValidationError as exc:
        raise CliError(str(exc)) from exc
    return payload


def run_command(client, name: str, args) -> int:
    """Execute exactly one API call and display its original result."""
    status, body = client.call(name, build_payload(name, args))
    return emit(status, body, args)


def run_health(client, args) -> int:
    status, body = client.healthz()
    return emit(status, body, args)


def run_batch(client, args) -> int:
    """Execute NDJSON records on one client, emitting one original result per call."""
    source = sys.stdin if args.input == "-" else open(args.input, encoding="utf-8")
    worst = 0
    try:
        for line in source:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or not isinstance(record.get("op"), str):
                    raise CliError("each NDJSON record must be an object with a string op")
                verb = record.pop("op")
                if verb not in api_method_names():
                    raise CliError(f"unknown MemoryAPI method: {verb!r}")
                parse_request(verb, record)
                status, body = client.call(verb, record)
            except (ValueError, CliError, ValidationError) as exc:
                status, body = 400, {"error": "ValidationError", "message": str(exc)}
            worst = max(worst, _write(status, body, "json", pretty=False))
    finally:
        if source is not sys.stdin:
            source.close()
    return worst


def emit(status: int, body: Any, args) -> int:
    """Write the API JSON value using the selected presentation format."""
    return _write(status, body, args.output, pretty=args.pretty)


def _write(status: int, body: Any, fmt: str, pretty: bool) -> int:
    ok = 200 <= status < 300
    stream = sys.stdout if ok else sys.stderr
    if fmt in ("text", "table"):
        text = _render_text(body)
    elif fmt == "quiet":
        text = _render_quiet(body)
    else:
        text = json.dumps(body, indent=2 if pretty else None, ensure_ascii=False)
    stream.write(text + "\n")
    return 0 if ok else 1


def _items(body: Any) -> list[Any]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        return body["items"]
    return [body]


def _render_text(body: Any) -> str:
    if isinstance(body, dict) and "error" in body:
        return f"error: {body['error']}: {body.get('message', '')}"
    lines = []
    for item in _items(body):
        if isinstance(item, dict) and ("id" in item or "unit_id" in item):
            content = item.get("content")
            if content is None:
                content = "\n".join(
                    segment.get("content", "") for segment in item.get("segments", [])
                )
            score = f"{item['score']:.4f}  " if "score" in item else ""
            lines.append(f"{score}{item.get('id', item.get('unit_id', ''))}  {content}".rstrip())
        else:
            lines.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(lines)


def _render_quiet(body: Any) -> str:
    values = []
    for item in _items(body):
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, dict):
            identifier = item.get("id", item.get("unit_id"))
            if identifier is not None:
                values.append(str(identifier))
    return "\n".join(values)
