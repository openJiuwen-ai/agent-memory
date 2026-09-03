# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""agent-memory CLI — the command-line surface over the memory engine.

A §15 surface (protocol adapter): it parses argv into a ``(verb, payload)`` and
hands it to an :class:`~client.EngineClient`, reusing the same dispatch the HTTP
surface uses. No business logic lives here. The verb + flag vocabulary tracks
common memory-layer CLI conventions (see ``DESIGN.md`` § "CLI compatibility").

通过启动脚本运行，以便把仓库根与 ``jiuwen_memory_entry/core`` 放入 ``PYTHONPATH``，
并确保 ``import server`` 解析到共享应用核 ``jiuwen_memory_entry/core/server.py``::

    scripts/run-cli.sh [global opts] <verb> [verb opts]

Two backends, chosen by ``--server`` / ``--base-url`` (else in-process):

    scripts/run-cli.sh add "buy milk" -u alice
    scripts/run-cli.sh search "milk" -u alice -k 3 -o text
    scripts/run-cli.sh --server http://127.0.0.1:8080 list -u alice
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from importlib import import_module

# Run-as-script: ensure this directory is an import root for the sibling CLI
# modules (client/commands), mirroring how the core/http_server modules flat-import.
_CLI_DIR = os.path.dirname(os.path.abspath(__file__))
if _CLI_DIR not in sys.path:
    sys.path.append(_CLI_DIR)

commands = import_module("commands")
make_client = import_module("client").make_client
CliError = commands.CliError


logger = logging.getLogger("agent-memory.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-memory",
        description="agent-memory memory engine CLI",
    )
    parser.add_argument(
        "--server",
        "--base-url",
        dest="server",
        metavar="URL",
        default=os.environ.get("AGENT_MEMORY_SERVER"),
        help="drive a running server over HTTP (default: in-process)",
    )
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="PATH",
        help="JSON config layer stacked on OFFLINE (in-process only; repeatable)",
    )
    parser.add_argument(
        "--api-key",
        dest="api_key",
        metavar="KEY",
        default=None,
        help="API key for --server mode (default: $AGENT_MEMORY_API_KEY)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    for name, cmd in commands.COMMANDS.items():
        sp = sub.add_parser(name, help=cmd.help)
        cmd.add_arguments(sp)

    for alias in ("health", "status"):
        hp = sub.add_parser(alias, help="liveness probe (GET /healthz)")
        commands.add_output_args(hp)

    batch = sub.add_parser("batch", help="run NDJSON ops on one stateful client (LoCoMo ingest)")
    batch.add_argument(
        "--input",
        default="-",
        help="NDJSON file of {op, ...payload}; '-' for stdin",
    )
    commands.add_output_args(batch)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s %(message)s",
    )
    args = build_parser().parse_args(argv)

    if args.server and args.config:
        logger.info("note: --config is ignored in --server (HTTP) mode")

    try:
        client = make_client(args.server, args.config, args.api_key)
        if args.command in ("health", "status"):
            return commands.run_health(client, args)
        if args.command == "batch":
            return commands.run_batch(client, args)
        return commands.run_command(client, args.command, args)
    except CliError as exc:
        logger.error("error: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
