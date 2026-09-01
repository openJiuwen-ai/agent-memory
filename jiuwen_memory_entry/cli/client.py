# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Engine clients for the CLI surface — one in-process, one over HTTP.

Both expose the same shape::

    call(verb: str, payload: dict) -> tuple[int, dict]

so the command layer (and any harness, e.g. the LoCoMo evaluation) is written
once against :class:`EngineClient` and chooses a backend at the edge.

Two backends, mirroring the two ways to reach an assembled memory engine:

- :class:`InProcessClient` builds the engine in *this* process (like
  ``jiuwen_memory_entry/http_server/__main__``) and routes through :func:`handler.dispatch` —
  the exact code path the HTTP surface uses, minus the socket. The engine lives
  for the client's lifetime, so repeated ``add`` calls share the in-memory store
  (the stateful path the LoCoMo ingest needs without a running server).
- :class:`HttpClient` POSTs to a running ``jiuwen_memory_entry`` server's ``/v1/<verb>``.

The CLI is a §15 *surface*: a protocol adapter that reuses the kernel's dispatch
and adds no business logic of its own.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Protocol

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory_entry.core.legacy_request_adapter import build_legacy_dispatch_request

# The shared core modules (server.py, handler.py, profiles.py) are a flat import
# root living in ``jiuwen_memory_entry/core``. Add it to the path so in-process mode can
# reuse the shared dispatch modules.
_CORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)


class EngineClient(Protocol):
    """A backend the CLI can drive: turn a (verb, payload) into (status, body)."""

    def call(self, verb: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        ...

    def healthz(self) -> tuple[int, dict[str, Any]]:
        ...


class InProcessClient:
    """Assemble an engine in this process and route verbs through it.

    ``configs`` are paths to JSON config layers stacked on top of the OFFLINE
    profile (nearest-wins), matching ``jiuwen_memory_entry/http_server/__main__``. The built
    :class:`Server` is held for the client's lifetime so writes persist across
    calls within the process.
    """

    def __init__(self, configs: list[str] | None = None) -> None:
        import server  # type: ignore[reportMissingImports]
        from profiles import OFFLINE, load_config  # type: ignore[reportMissingImports]

        spaces = server.default_spaces()
        layers: list[dict] = [OFFLINE]
        for path in configs or []:
            with open(path, "r", encoding="utf-8") as fh:
                layers.append(json.load(fh))
        config = load_config(layers, spaces)
        self._srv = server.build(config, spaces=spaces)

    @property
    def server(self):
        """The assembled :class:`jiuwen_memory_entry.server.Server` (for direct inspection)."""
        return self._srv

    def call(self, verb: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self._srv.dispatch(verb, payload)

    def healthz(self) -> tuple[int, dict[str, Any]]:
        return 200, {"status": "ok", "profile": self._srv.config.profile}

    def close(self) -> None:
        self._srv.close(wait=True)


class HttpClient:
    """Drive a running ``jiuwen_memory_entry`` server over HTTP (``POST /v1/<verb>``)."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = (
            api_key if api_key is not None else os.environ.get("AGENT_MEMORY_API_KEY", "")
        )
        self.api_key = self.api_key.strip()

    def call(self, verb: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self._request("POST", f"/v1/{verb}", to_http_payload(verb, payload))

    def healthz(self) -> tuple[int, dict[str, Any]]:
        return self._request("GET", "/healthz", None)

    def _request(self, method: str, path: str, body: dict | None) -> tuple[int, dict[str, Any]]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, _read_json(resp)
        except urllib.error.HTTPError as exc:
            # Domain errors come back as a JSON body with a non-2xx status.
            return exc.code, _read_json(exc)
        except urllib.error.URLError as exc:
            return 0, {"error": "ConnectionError", "message": str(exc.reason)}


def _read_json(resp) -> dict[str, Any]:
    raw = resp.read()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"error": "BadResponse", "message": raw.decode("utf-8", "replace")}


def _scope_payload(scope: Scope) -> dict[str, str]:
    """Serialize one already-parsed Scope for the HTTP DTO wire format."""
    payload: dict[str, str] = {}
    for key, value in (
        ("tenant_id", scope.org),
        ("space", scope.space),
        ("scope", scope.user),
        ("agent", scope.agent),
        ("session", scope.session),
    ):
        if value:
            payload[key] = value
    return payload


def to_http_payload(verb: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Serialize legacy input through the shared compatibility adapter.

    Flat Scope interpretation lives exclusively in ``legacy_request_adapter``;
    this function only turns its typed output into the strict HTTP DTO shape.
    """
    if "target" in payload:
        return dict(payload)
    request = build_legacy_dispatch_request(verb, payload)
    body = dict(request.payload)
    body.pop("defaults", None)
    has_target = any(
        key in payload for key in ("tenant_id", "scope", "space", "space_id", "agent", "session")
    )
    if (has_target or verb == "batch_add") and request.target is not None:
        body["target"] = _scope_payload(request.target)
    if request.grantee is not None:
        body["grantee"] = _scope_payload(request.grantee)
    if request.member is not None:
        body["member"] = _scope_payload(request.member)
    if request.batch_items:
        body["items"] = [
            {
                **dict(item.payload),
                **({"target": _scope_payload(item.target)} if item.target is not None else {}),
            }
            for item in request.batch_items
        ]
        defaults = payload.get("defaults")
        if isinstance(defaults, dict):
            # Batch defaults are serialized a second time so legacy flat Scope fields
            # become the strict HTTP DTO's nested target shape.
            default_request = build_legacy_dispatch_request("add", defaults)
            default_body = dict(default_request.payload)
            body["defaults"] = default_body
    return body


def make_client(server_url: str | None, configs: list[str] | None = None) -> EngineClient:
    """Pick a backend: HTTP when ``server_url`` is given, else in-process."""
    if server_url:
        return HttpClient(server_url)
    return InProcessClient(configs)
