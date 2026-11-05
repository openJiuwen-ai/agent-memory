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

from deploy.migration.runner import migrate_plaintext_memories
from jiuwen_memory.common.security.types import Credentials
from jiuwen_memory.config import Config

MAX_LINE_BYTES = 64 * 1024 * 1024


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
    parser.add_argument("--config", required=True, help="Trusted kernel YAML configuration")
    parser.add_argument("--input", help="Optional exported plaintext MemoryUnit JSONL file")
    parser.add_argument("--operation", required=True, choices=("inspect", "migrate", "clear"))
    parser.add_argument("--port", help="Restrict to a configured encrypted KV port")
    args = parser.parse_args(argv)
    try:
        config = Config.from_yaml(args.config)
        with open(args.input, "rb") if args.input else nullcontext(None) as stream:
            result = migrate_plaintext_memories(
                _records(stream) if stream is not None else None,
                config=config,
                credentials=Credentials(api_key=os.environ.get("AGENT_MEMORY_API_KEY", "")),
                operation=args.operation,
                port=args.port,
            )
        body = asdict(result)
        code = 0 if result.status == "complete" else 2
    except Exception:
        body = {"operation": args.operation, "status": "failed", "error": "input_failed"}
        code = 2
    # This command's stdout is a machine-readable result, isolated from kernel logs.
    output = logging.Logger(__name__)
    output.addHandler(logging.StreamHandler(sys.stdout))
    output.info(json.dumps(body, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
