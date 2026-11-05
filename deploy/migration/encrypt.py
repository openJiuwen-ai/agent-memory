# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit authenticated storage scan/migrate/clear, with optional MemoryUnit JSONL input."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import nullcontext
from dataclasses import asdict

import yaml

from deploy.migration.runner import migrate_plaintext_memories
from jiuwen_memory.common.errors import ValidationError, safe_error_message
from jiuwen_memory.common.security.types import Credentials
from jiuwen_memory.config import Config

MAX_LINE_BYTES = 64 * 1024 * 1024


class _ConfigurationInputError(ValueError):
    """Actionable configuration diagnostics without echoing YAML source or secrets."""


def _load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as stream:
        text = stream.read()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise _ConfigurationInputError(f"Invalid configuration YAML{location}") from exc
    if not isinstance(data, dict):
        raise _ConfigurationInputError("Configuration must be a YAML mapping")
    if "memory_api" in data:
        data = data["memory_api"]
        if not isinstance(data, dict):
            raise _ConfigurationInputError("Service configuration memory_api must be a mapping")
    elif "profile" in data:
        raise _ConfigurationInputError(
            "Service configuration requires a memory_api mapping; "
            "alternatively supply standalone kernel YAML"
        )
    try:
        # Expand only the selected kernel section through the existing public parser.
        config = Config.from_yaml_str(yaml.safe_dump(data))
        config.context()
    except ValidationError as exc:
        raise _ConfigurationInputError(f"Invalid configuration: {safe_error_message(exc)}") from exc
    except (TypeError, ValueError) as exc:
        raise _ConfigurationInputError(
            "Invalid kernel configuration layout; check globals and plugin instance mappings"
        ) from exc
    return config


def _records(stream):
    while True:
        raw = stream.readline(MAX_LINE_BYTES + 2)
        if not raw:
            return
        if len(raw) > MAX_LINE_BYTES + 1:
            raise ValueError("input record exceeds limit")
        # Preserve serialized bytes: the newline is the export container separator.
        yield raw.removesuffix(b"\n").removesuffix(b"\r")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True, help="Trusted kernel YAML or service YAML with memory_api"
    )
    parser.add_argument("--input", help="Optional exported plaintext MemoryUnit JSONL file")
    parser.add_argument("--operation", required=True, choices=("inspect", "migrate", "clear"))
    parser.add_argument("--port", help="Restrict to a configured encrypted KV port")
    parser.add_argument("--cursor", help="Resume a storage scan from the previous result cursor")
    parser.add_argument(
        "--batch-size", type=int, default=10000, help="Scan records per run, 1..10000"
    )
    parser.add_argument(
        "--batch-bytes", type=int, default=268435456, help="Plaintext scan bytes per run"
    )
    args = parser.parse_args(argv)
    try:
        config = _load_config(args.config)
        with open(args.input, "rb") if args.input else nullcontext(None) as stream:
            result = migrate_plaintext_memories(
                _records(stream) if stream is not None else None,
                config=config,
                credentials=Credentials(api_key=os.environ.get("AGENT_MEMORY_API_KEY", "")),
                operation=args.operation,
                port=args.port,
                cursor=args.cursor,
                batch_size=args.batch_size,
                batch_bytes=args.batch_bytes,
            )
        body = asdict(result)
        if result.cursor:
            body["message"] = "Resume with --cursor and the same configuration, operation and port"
        code = 0 if result.status == "complete" else 2
    except _ConfigurationInputError as exc:
        body = {
            "operation": args.operation,
            "status": "failed",
            "error": "invalid_input",
            "message": str(exc),
        }
        code = 2
    except OSError:
        body = {
            "operation": args.operation,
            "status": "failed",
            "error": "input_failed",
            "message": "Cannot read --config or --input; check file existence and permissions",
        }
        code = 2
    except Exception:
        body = {
            "operation": args.operation,
            "status": "failed",
            "error": "input_failed",
            "message": "Cannot process input; check configuration and JSONL input files",
        }
        code = 2
    # This command's stdout is a machine-readable result, isolated from kernel logs.
    output = logging.Logger(__name__)
    output.addHandler(logging.StreamHandler(sys.stdout))
    output.info(json.dumps(body, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
