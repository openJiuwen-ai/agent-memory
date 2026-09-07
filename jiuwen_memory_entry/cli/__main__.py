# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CLI entry: same-named MemoryAPI methods, JSON objects, and original results.

    scripts/run-cli.sh --auth-mode dev add --content "buy milk" \
        --scope '{"org":"local","user":"developer"}'
    scripts/run-cli.sh --server http://127.0.0.1:8137 list \
        --scope '{"org":"local","user":"developer"}'
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from jiuwen_memory_entry.cli import commands
from jiuwen_memory_entry.cli.client import make_client
from jiuwen_memory_entry.core.api_contract import api_method_names

logger = logging.getLogger("agent-memory.cli")


def build_parser() -> argparse.ArgumentParser:
    """Build CLI methods and options directly from the public API contract."""
    parser = argparse.ArgumentParser(prog="agent-memory", allow_abbrev=False)
    parser.add_argument(
        "--server",
        default=os.environ.get("AGENT_MEMORY_SERVER"),
        help="HTTP endpoint; omitted means in-process execution",
    )
    parser.add_argument(
        "--config", action="append", default=[], help="local JSON/YAML config layer"
    )
    parser.add_argument(
        "--auth-mode",
        choices=("required", "dev"),
        default="required",
        help="local authentication mode; dev uses the fixed local/developer test identity",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in sorted(api_method_names()):
        method_parser = sub.add_parser(name, help=f"MemoryAPI.{name}", allow_abbrev=False)
        commands.add_api_arguments(method_parser, name)
    health = sub.add_parser("healthz", help="liveness probe", allow_abbrev=False)
    commands.add_output_args(health)
    batch = sub.add_parser(
        "batch", help="execute NDJSON API calls in one session", allow_abbrev=False
    )
    batch.add_argument("--input", default="-", help='NDJSON {"op": "<method>", ...API parameters}')
    commands.add_output_args(batch)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a command and always close the client runtime."""
    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(name)s %(levelname)s %(message)s"
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.server and args.auth_mode == "dev":
        parser.error("--auth-mode dev applies to local execution; configure dev on the HTTP server")
    if args.server and args.config:
        parser.error("--config applies only to local execution")
    client = None
    try:
        client = make_client(args.server, args.config, auth_mode=args.auth_mode)
        if args.command == "healthz":
            return commands.run_health(client, args)
        if args.command == "batch":
            return commands.run_batch(client, args)
        return commands.run_command(client, args.command, args)
    except (commands.CliError, OSError) as exc:
        logger.error("error: %s", exc)
        return 2
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
