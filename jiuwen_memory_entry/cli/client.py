# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CLI clients share MemoryAPI JSON parameters and unwrapped return values."""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
import uuid
from types import SimpleNamespace
from typing import Any, Protocol

from jiuwen_memory.api import AgentMemoryError, Credentials, Surface
from jiuwen_memory_entry.core.api_contract import invoke_api, is_known_verb
from jiuwen_memory_entry.core.auth_middleware import authenticated
from jiuwen_memory_entry.core.dev_security import with_local_dev_security
from jiuwen_memory_entry.core.error_response import error_response

_CORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

logger = logging.getLogger("agent-memory.cli")


class EngineClient(Protocol):
    """Call an API method with JSON parameters and return status plus JSON value."""

    def call(self, verb: str, payload: dict[str, Any]) -> tuple[int, Any]:
        """Invoke an API method with JSON parameters."""
        ...

    def healthz(self) -> tuple[int, dict[str, Any]]:
        """Return the liveness status and its JSON value."""
        ...

    def close(self) -> None:
        """Release resources owned by the client."""
        ...


class InProcessClient:
    """Hold one runtime and directly invoke MemoryAPI with an injected authenticator."""

    def __init__(
        self,
        configs: list[str] | None = None,
        *,
        authenticator: Any = None,
        dev_authentication: bool = False,
    ) -> None:
        """Assemble one runtime; identity never comes from business parameters."""
        from config_loader import load_layer
        from profiles import OFFLINE, load_config
        from server import Server

        config = load_config([OFFLINE, *(load_layer(p) for p in configs or [])])
        if dev_authentication:
            config = with_local_dev_security(config)
        if authenticator is None:
            self._srv = Server.build(config)
        else:
            self._srv = Server.build(
                config,
                security_runtime=SimpleNamespace(authenticator=authenticator),
            )

    @property
    def server(self):
        """Return the assembled surface server for lifecycle and API access."""
        return self._srv

    def call(self, verb: str, payload: dict[str, Any]) -> tuple[int, Any]:
        """Decode parameters, authenticate, and call the same-named API method."""
        request_id = uuid.uuid4().hex
        error: object = "SecurityUnavailable"
        detail: object = ""
        try:
            if not is_known_verb(verb):
                error = "UnknownVerb"
            else:
                runtime = self._srv.security_runtime
                if runtime is not None:
                    credentials = Credentials(api_key=os.environ.get("AGENT_MEMORY_API_KEY", ""))
                    guard = getattr(runtime, "workload_guard", None)
                    if not runtime.authenticator.requires_concurrency_guard():
                        guard = None
                    with authenticated(
                        runtime.authenticator,
                        credentials,
                        audit=self._srv.audit,
                        workload_guard=guard,
                        surface=Surface.CLI,
                        request_id=request_id,
                    ) as security:
                        return 200, invoke_api(self._srv.api, verb, payload, security)
        except AgentMemoryError as exc:
            error, detail = type(exc), exc
        except Exception as exc:
            error = "InternalError"
            logger.error(
                "CLI request failed request_id=%s error_type=%s", request_id, type(exc).__name__
            )
        status, body, _ = error_response(error, detail)
        if error == "SecurityUnavailable":
            body["message"] = (
                "CLI authentication is not configured; use --auth-mode dev for testing"
            )
        body["request_id"] = request_id
        return status, body

    def healthz(self) -> tuple[int, dict[str, Any]]:
        return 200, {"status": "ok", "profile": self._srv.config.profile}

    def close(self) -> None:
        self._srv.close(wait=True)


class HttpClient:
    """Send API-shaped requests and return the HTTP server's original JSON value."""

    def __init__(self, base_url: str, timeout: float = 30.0, api_key: str | None = None) -> None:
        """Configure the endpoint and credentials without changing API payloads."""
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = (
            api_key if api_key is not None else os.environ.get("AGENT_MEMORY_API_KEY", "")
        ).strip()

    def call(self, verb: str, payload: dict[str, Any]) -> tuple[int, Any]:
        if not is_known_verb(verb):
            status, body, _ = error_response("UnknownVerb")
            return status, body
        return self._request("POST", f"/v1/{verb}", payload)

    def healthz(self) -> tuple[int, dict[str, Any]]:
        return self._request("GET", "/healthz", None)

    @staticmethod
    def close() -> None:
        """HTTP requests own and close their individual connections."""

    def _request(self, method: str, path: str, body: dict | None) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, _read_json(response)
        except urllib.error.HTTPError as exc:
            return exc.code, _read_json(exc)
        except urllib.error.URLError as exc:
            return 0, {"error": "ConnectionError", "message": str(exc.reason)}


def _read_json(response) -> Any:
    raw = response.read()
    try:
        return json.loads(raw)
    except ValueError:
        return {"error": "BadResponse", "message": "server returned invalid JSON"}


def make_client(
    server_url: str | None, configs: list[str] | None = None, *, auth_mode: str = "required"
) -> EngineClient:
    """Select HTTP or in-process execution; dev authentication is explicit and local."""
    if auth_mode not in {"required", "dev"}:
        raise ValueError(f"unknown authentication mode: {auth_mode}")
    if server_url:
        return HttpClient(server_url)
    dev_authentication = auth_mode == "dev"
    if dev_authentication:
        logger.warning("development authentication is enabled for local CLI testing")
    return InProcessClient(configs, dev_authentication=dev_authentication)
