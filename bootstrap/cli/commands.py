"""Subcommands for the CLI surface — argument parsing, payload building, output.

The verb set and flag names track common memory-layer CLI conventions
(`add/search/list/get/update/delete`, `-u/--user-id`, positional content/id,
`-k/--top-k`, `-o/--output`, `--messages`/`--file`, `--categories`, `--all`) so
users familiar with those CLIs can drive agent-memory with the same muscle
memory. Where our engine's contract differs (a mandatory `tenant_id`, the two
id spaces), we adapt rather than fake — see ``DESIGN.md`` § "CLI compatibility".

Each engine verb is a row in :data:`COMMANDS` mapping it to
``(add_arguments, build_payload)``; the subparser is built from the row and the
payload assembled from parsed args. Adding a verb is adding a row, never editing a
dispatch ``if/else`` (the A20 "route by table" rule the engine follows).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("agent-memory.cli.commands")


class CliError(Exception):
    """A user-input error (missing scope, bad JSON) — reported, not a traceback."""


@dataclass(frozen=True)
class Command:
    verb: str
    help: str
    add_arguments: Callable[[Any], None]
    build_payload: Callable[[Any], dict[str, Any]]


# --- shared argument groups (compat-style identity + output) --------------

def add_identity_args(p) -> None:
    """Compat-style scoping: ``-u/--user-id`` is primary; ``--scope`` is the native
    alias; ``--tenant`` is our multi-tenant dimension (optional, defaults).
    """
    p.add_argument("-u", "--user-id", dest="user_id", help="scope to a user (--user-id)")
    p.add_argument("--scope", help="native alias of --user-id")
    p.add_argument("--tenant", help="tenant_id (default: $AGENT_MEMORY_TENANT or 'default')")
    p.add_argument("--trace", default="", help="optional trace_id")


def add_output_args(p) -> None:
    p.add_argument("-o", "--output", choices=["json", "text", "table", "quiet"],
                   help="output format (default: json)")
    p.add_argument("--json", "--agent", dest="json", action="store_true",
                   help="force JSON output (--json/--agent)")
    p.add_argument("--pretty", action="store_true", help="pretty-print JSON")


def triple(args) -> dict[str, Any]:
    """Resolve the mandatory (tenant, scope, trace) from compat/native flags + env."""
    scope = getattr(args, "user_id", None) or getattr(args, "scope", None) \
        or os.environ.get("AGENT_MEMORY_USER_ID", "")
    if not scope:
        raise CliError("a scope is required — pass -u/--user-id (or --scope), "
                       "or set $AGENT_MEMORY_USER_ID")
    tenant = getattr(args, "tenant", None) or os.environ.get("AGENT_MEMORY_TENANT", "") or "default"
    return {"tenant_id": tenant, "scope": scope, "trace_id": getattr(args, "trace", "") or ""}


# --- value helpers --------------------------------------------------------

def _tags(value: str | None) -> list[str] | None:
    """Parse ``--tags``/``--categories``: a JSON array or a comma-separated list."""
    if value is None:
        return None
    value = value.strip()
    if value.startswith("["):
        try:
            return [str(t) for t in json.loads(value)]
        except ValueError as exc:
            raise CliError(f"--categories/--tags: bad JSON array ({exc})") from exc
    tags = []
    for raw_tag in value.split(","):
        tag = raw_tag.strip()
        if tag:
            tags.append(tag)
    return tags


def _flatten_messages(raw: str) -> str:
    """Turn a ``--messages`` JSON ``[{role, content}, ...]`` into one memory."""
    try:
        msgs = json.loads(raw)
    except ValueError as exc:
        raise CliError(f"--messages: bad JSON ({exc})") from exc
    if isinstance(msgs, str):
        return msgs
    lines = []
    for m in msgs:
        if isinstance(m, dict):
            lines.append(f"{m.get('role', 'user')}: {m.get('content', '')}")
        else:
            lines.append(str(m))
    return "\n".join(lines)


def _read_source(path: str) -> str:
    """Read ``--file`` (``-`` = stdin). JSON message arrays are flattened."""
    data = sys.stdin.read() if path == "-" else open(path, "r", encoding="utf-8").read()
    stripped = data.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        return _flatten_messages(stripped)
    return data.strip()


def _resolve_content(args) -> str:
    """Resolve ``add`` content: positional text, ``--content``, ``--messages``, or ``--file``."""
    if getattr(args, "content", None):
        return args.content
    if getattr(args, "text", None):
        return args.text
    if getattr(args, "messages", None):
        return _flatten_messages(args.messages)
    if getattr(args, "file", None):
        return _read_source(args.file)
    raise CliError("nothing to add — give text, --content, --messages, or --file")


# --- per-verb argument + payload builders ---------------------------------

def _args_add(p) -> None:
    add_identity_args(p)
    p.add_argument("text", nargs="?", help="memory text (positional)")
    p.add_argument("-c", "--content", help="memory text (alias of the positional)")
    p.add_argument("--messages", help="JSON array of {role, content}")
    p.add_argument("-f", "--file", help="read text/JSON messages from a file ('-' = stdin)")
    p.add_argument("--tags", help="comma-separated or JSON-array tags")
    p.add_argument("--categories", help="alias of --tags")
    p.add_argument("--owner-ref", default="", help="optional owner reference")
    p.add_argument("--modality", default="text", choices=["text", "image", "audio"])
    add_output_args(p)


def _payload_add(args) -> dict[str, Any]:
    payload = triple(args)
    payload.update(content=_resolve_content(args), owner_ref=args.owner_ref, modality=args.modality)
    tags = _tags(args.tags if args.tags is not None else args.categories)
    if tags is not None:
        payload["tags"] = tags
    return payload


def _args_search(p) -> None:
    add_identity_args(p)
    p.add_argument("query_pos", nargs="?", metavar="query", help="query text (positional)")
    p.add_argument("-q", "--query", help="query text (alias of the positional)")
    p.add_argument("-k", "--top-k", dest="top_k", type=int, help="number of results (--top-k)")
    p.add_argument("--k", dest="k", type=int, help="native alias of --top-k")
    p.add_argument("--threshold", type=float, help="drop hits below this score")
    p.add_argument("--filters", help="JSON object of equality filters")
    add_output_args(p)


def _payload_search(args) -> dict[str, Any]:
    query = args.query or args.query_pos
    if not query:
        raise CliError("a query is required — give it positionally or with --query")
    k = args.top_k if args.top_k is not None else (args.k if args.k is not None else 10)
    payload = {**triple(args), "query": query, "k": k}
    if args.filters:
        try:
            payload["filters"] = json.loads(args.filters)
        except ValueError as exc:
            raise CliError(f"--filters: bad JSON ({exc})") from exc
    return payload


def _args_list(p) -> None:
    add_identity_args(p)
    add_output_args(p)


def _payload_list(args) -> dict[str, Any]:
    return triple(args)


def _id_arg(args) -> str:
    item_id = getattr(args, "item_id", None) or getattr(args, "id_pos", None)
    if not item_id:
        raise CliError("a memory id is required — give it positionally or with --item-id")
    return item_id


def _args_get(p) -> None:
    add_identity_args(p)
    p.add_argument("id_pos", nargs="?", metavar="memory-id", help="lifecycle id (positional)")
    p.add_argument("--item-id", dest="item_id", help="alias of the positional id")
    add_output_args(p)


def _payload_get(args) -> dict[str, Any]:
    return {**triple(args), "item_id": _id_arg(args)}


def _args_update(p) -> None:
    add_identity_args(p)
    p.add_argument("id_pos", nargs="?", metavar="memory-id", help="lifecycle id (positional)")
    p.add_argument("text", nargs="?", help="new content (positional)")
    p.add_argument("--item-id", dest="item_id", help="alias of the positional id")
    p.add_argument("-c", "--content", help="new content (alias of the positional)")
    p.add_argument("--tags", help="new comma-separated or JSON-array tags")
    p.add_argument("--categories", help="alias of --tags")
    add_output_args(p)


def _payload_update(args) -> dict[str, Any]:
    payload = {**triple(args), "item_id": _id_arg(args)}
    content = args.content if args.content is not None else args.text
    if content is not None:
        payload["content"] = content
    tags = _tags(args.tags if args.tags is not None else args.categories)
    if tags is not None:
        payload["tags"] = tags
    return payload


def _args_delete(p) -> None:
    add_identity_args(p)
    p.add_argument("id_pos", nargs="?", metavar="memory-id", help="lifecycle id (positional)")
    p.add_argument("--item-id", dest="item_id", help="alias of the positional id")
    p.add_argument("--all", action="store_true", help="delete all memories in scope (--all)")
    p.add_argument(
        "--force",
        action="store_true",
        help="skip confirmation (compat parity)",
    )
    p.add_argument("--hard", action="store_true", help="hard delete (needs --approval-token)")
    p.add_argument("--approval-token", default="", help="approval token for a hard delete")
    add_output_args(p)


def _payload_delete(args) -> dict[str, Any]:
    return {
        **triple(args),
        "item_id": _id_arg(args),
        "hard": args.hard,
        "approval_token": args.approval_token,
    }


COMMANDS: dict[str, Command] = {
    "add": Command("add", "store a memory (indexed for search)", _args_add, _payload_add),
    "search": Command("search", "semantic search (L4)", _args_search, _payload_search),
    "list": Command("list", "list memories in scope", _args_list, _payload_list),
    "get": Command("get", "fetch one memory by id", _args_get, _payload_get),
    "update": Command("update", "partial-update a memory", _args_update, _payload_update),
    "delete": Command(
        "delete",
        "soft/hard delete a memory (or --all)",
        _args_delete,
        _payload_delete,
    ),
}


# --- execution ------------------------------------------------------------

def run_command(client, name: str, args) -> int:
    cmd = COMMANDS[name]

    # `delete --all`: our engine has only per-item delete, so fan out over a
    # `list` client-side (a CLI ergonomic, not an engine verb).
    if name == "delete" and getattr(args, "all", False):
        return _delete_all(client, args)

    status, body = client.call(cmd.verb, cmd.build_payload(args))

    # `search --threshold`: filter hits client-side, preserving the envelope.
    if name == "search" and getattr(args, "threshold", None) is not None and status == 200:
        body = dict(body)
        body["hits"] = [h for h in body.get("hits", []) if h.get("score", 0.0) >= args.threshold]

    return emit(status, body, args)


def _delete_all(client, args) -> int:
    t = triple(args)
    status, body = client.call("list", t)
    if status != 200:
        return emit(status, body, args)
    items = body.get("items", [])
    deleted = []
    for item in items:
        st, _ = client.call("delete", {
            **t, "item_id": item["item_id"], "hard": args.hard,
            "approval_token": args.approval_token,
        })
        if 200 <= st < 300:
            deleted.append(item["item_id"])
    return emit(200, {"ok": True, "op": "delete_all", "deleted": deleted,
                      "count": len(deleted)}, args)


def run_health(client, args) -> int:
    status, body = client.healthz()
    return emit(status, body, args)


def run_batch(client, args) -> int:
    """Run a stream of NDJSON ops on one (stateful) client — the LoCoMo ingest
    primitive and a stateful-session demo. Always emits one JSON result per op
    (machine-facing), regardless of ``--output``.
    """
    source = sys.stdin if args.input in (None, "-") else open(args.input, "r", encoding="utf-8")
    worst = 0
    try:
        for lineno, line in enumerate(source, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                verb = record.pop("op")
            except (ValueError, KeyError) as exc:
                logger.warning("line %s: bad record (%s)", lineno, exc)
                worst = max(worst, 1)
                continue
            status, body = client.call(verb, record)
            worst = max(worst, _write(status, body, "json", pretty=False))
    finally:
        if source is not sys.stdin:
            source.close()
    return worst


# --- output ---------------------------------------------------------------

def _resolve_format(args) -> str:
    if getattr(args, "json", False):
        return "json"
    return getattr(args, "output", None) or "json"


def emit(status: int, body: dict[str, Any], args) -> int:
    return _write(status, body, _resolve_format(args), pretty=getattr(args, "pretty", False))


def _write(status: int, body: dict[str, Any], fmt: str, pretty: bool) -> int:
    ok = 200 <= status < 300
    stream = sys.stdout if ok else sys.stderr
    if fmt in ("text", "table"):
        text = _render_text(body)
    elif fmt == "quiet":
        text = _render_quiet(body)
    elif pretty:
        text = json.dumps(body, indent=2, ensure_ascii=False)
    else:
        text = json.dumps(body, ensure_ascii=False)
    stream.write(text + "\n")
    return 0 if ok else 1


def _render_text(body: dict[str, Any]) -> str:
    if "error" in body:
        return f"error: {body['error']}: {body.get('message', '')}"
    if "status" in body:  # healthz
        return f"{body.get('status')} (profile={body.get('profile', '')})"
    if body.get("op") == "delete_all":
        return f"deleted {body.get('count', 0)} memories"
    lines: list[str] = []
    if body.get("hits"):
        for h in body["hits"]:
            lines.append(
                (
                    f"{h.get('score', 0.0):.4f}  "
                    f"{h.get('item_id', '')}  "
                    f"{h.get('content', '')}"
                ).rstrip()
            )
    if body.get("items"):
        for it in body["items"]:
            tags = f" {it.get('tags')}" if it.get("tags") else ""
            lines.append(f"{it.get('item_id', '')}  {it.get('content', '')}{tags}")
    if body.get("item"):
        it = body["item"]
        tags = f" {it.get('tags')}" if it.get("tags") else ""
        lines.append(f"{it.get('item_id', '')}  {it.get('content', '')}{tags}")
    if not lines:
        lines.append(
            f"ok ({body.get('op', '')})"
            if body.get("ok")
            else json.dumps(body, ensure_ascii=False)
        )
    return "\n".join(lines)


def _render_quiet(body: dict[str, Any]) -> str:
    if "error" in body:
        return ""
    if body.get("hits"):
        return "\n".join(h.get("item_id", "") for h in body["hits"])
    if body.get("items"):
        return "\n".join(it.get("item_id", "") for it in body["items"])
    if body.get("op") == "delete_all":
        return "\n".join(body.get("deleted", []))
    if body.get("item"):
        return body["item"].get("item_id", "")
    return body.get("item_id", "")
