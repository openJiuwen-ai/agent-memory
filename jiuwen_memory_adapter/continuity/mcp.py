"""MCP bridge: remote service when configured, local service otherwise (F17 D10).

Remote mode keeps the existing behaviour (``AGENT_MEMORY_URL`` + ``AGENT_MEMORY_API_KEY``).
Local mode discovers the machine's service (starting it when needed), authenticates
with the install token named by ``JIUWEN_MEMORY_INSTALL`` and keeps a lease alive so the
service stays up while this host runs. Agent surfaces only get participation
operations: task close, binding for other connectors and attach stay CLI/Portal-only.
"""

import asyncio
import json
import logging
import os
import threading

from jiuwen_memory_adapter.continuity import tool_schemas
from jiuwen_memory_adapter.continuity.client import ContinuityClient

logger = logging.getLogger(__name__)

_ADMIN_OPERATIONS = frozenset({"close", "attach"})
_LEASE_SECONDS = 20.0


class _Lease:
    """Renew the local service lease in the background until closed."""

    def __init__(self, client, install_id: str) -> None:
        self._client = client
        self._install_id = install_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._client.lease(self._install_id)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(_LEASE_SECONDS):
            try:
                self._client.lease(self._install_id)
            except Exception:
                logger.warning("continuity lease renewal failed", exc_info=True)

    def close(self) -> None:
        self._stop.set()
        try:
            self._client.release(self._install_id)
        except Exception:
            logger.debug("continuity lease release failed", exc_info=True)


class _LocalConnection:
    def __init__(
        self, discovery, token: str, install_id: str, connector_id: str = "",
        hooks: bool = False,
    ) -> None:
        from jiuwen_memory_entry.local.client import ServiceClient

        self.discovery = discovery
        self.base_url = f"http://127.0.0.1:{discovery.port}"
        self.token = token
        self.install_id = install_id
        self.connector_id = connector_id
        self.hooks = hooks
        self._client = ServiceClient(discovery, token)
        self.lease = _Lease(self._client, install_id)

    def start(self) -> None:
        self.lease.start()

    def close(self) -> None:
        self.lease.close()


def _connect():
    url = os.environ.get("AGENT_MEMORY_URL")
    if url:
        return (
            ContinuityClient(url, os.environ["AGENT_MEMORY_API_KEY"]),
            "remote",
            None,
        )
    from jiuwen_memory_entry.local.discovery import ensure_service
    from jiuwen_memory_entry.local.home import resolve_home

    home = resolve_home()
    install = os.environ.get("JIUWEN_MEMORY_INSTALL", "").strip()
    if not install:
        raise RuntimeError(
            "local MCP mode needs JIUWEN_MEMORY_INSTALL; run `jiuwen-memory connect` for "
            "this host first"
        )
    token_path = home.installs_dir / install / "token"
    if not token_path.exists():
        raise RuntimeError(f"unknown install {install!r}; run `jiuwen-memory connect` first")
    discovery = ensure_service(home)
    token = token_path.read_text(encoding="utf-8").strip()
    connector_id = ""
    hooks = False
    try:
        record = json.loads(
            (home.installs_dir / install / "install.json").read_text(encoding="utf-8")
        )
        connector_id = str(record.get("connector_id", ""))
        hooks = bool(record.get("hooks", str(record.get("host", "")) == "codex"))
    except (OSError, ValueError):
        connector_id = ""
    return (
        ContinuityClient(f"http://127.0.0.1:{discovery.port}", token),
        "local",
        _LocalConnection(discovery, token, install, connector_id, hooks),
    )


def schemas_for(mode: str, *, hooks: bool = False) -> list[dict]:
    """Remote deployments keep their existing tool list; only local adds add/search."""
    return tool_schemas(include_io=(mode == "local"), hooks=hooks)


async def run():
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    client, mode, local = _connect()
    if local is not None:
        local.start()
    schemas = schemas_for(
        mode, hooks=bool(getattr(local, "hooks", False)) if local is not None else False
    )
    by_name = {schema["name"]: schema for schema in schemas}

    server = Server("agent-memory-continuity")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=schema["name"],
                description=schema["description"],
                inputSchema=schema["parameters"],
            )
            for schema in schemas
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name not in by_name:
            raise ValueError(f"unknown tool: {name}")
        verb = name.removeprefix("agent_memory_")
        if mode == "local" and verb == "continuity_task":
            await _check_task_operation(client, local, arguments or {})
        result = await asyncio.to_thread(client.call, verb, arguments or {})
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        if local is not None:
            local.close()
        client.close()


async def _check_task_operation(client, local, arguments: dict) -> None:
    """Agents may bind/unbind their own connection, never manage another install's task."""
    request = arguments.get("request") if isinstance(arguments.get("request"), dict) else {}
    operation = str(request.get("operation", ""))
    if operation in _ADMIN_OPERATIONS or (
        operation == "unbind" and not request.get("binding_id")
    ):
        raise ValueError(
            f"operation {operation!r} is not available to agent surfaces; "
            "use the jiuwen-memory CLI or Portal"
        )
    own = getattr(local, "connector_id", "")
    if operation == "bind":
        if not own or request.get("connector_id") != own:
            raise ValueError("agents may only bind their own connection")
        return
    if operation == "unbind":
        scope = arguments.get("scope") or {}
        view = await asyncio.to_thread(
            client.call,
            "continuity_task",
            {"scope": scope,
             "request": {"operation": "get", "task_id": request.get("task_id", "")}},
        )
        if not isinstance(view, dict) or not isinstance(view.get("bindings"), list):
            raise ValueError(
                "task lookup failed; cannot verify which connection owns this binding"
            )
        bindings = view["bindings"]
        connectors = {
            str(item.get("binding_id")): str(item.get("connector_id"))
            for item in (bindings or [])
            if isinstance(item, dict)
        }
        if not own or connectors.get(str(request.get("binding_id", ""))) != own:
            raise ValueError("agents may only unbind their own connection")


if __name__ == "__main__":
    asyncio.run(run())
