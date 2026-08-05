"""Engine clients for the CLI surface — one in-process, one over HTTP.

Both expose the same shape::

    call(verb: str, payload: dict) -> tuple[int, dict]

so the command layer (and any harness, e.g. the LoCoMo evaluation) is written
once against :class:`EngineClient` and chooses a backend at the edge.

Two backends, mirroring the two ways to reach an assembled memory engine:

- :class:`InProcessClient` builds the engine in *this* process (like
  ``bootstrap/http_server/__main__``) and routes through :func:`handler.dispatch` —
  the exact code path the HTTP surface uses, minus the socket. The engine lives
  for the client's lifetime, so repeated ``add`` calls share the in-memory store
  (the stateful path the LoCoMo ingest needs without a running server).
- :class:`HttpClient` POSTs to a running ``bootstrap`` server's ``/v1/<verb>``.

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

# The shared core modules (server.py, handler.py, profiles.py) are a flat import
# root living in ``bootstrap/core``. Add it to the path so in-process mode can
# reuse the shared dispatch modules.
_CORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)


class EngineClient(Protocol):
    """A backend the CLI can drive: turn a (verb, payload) into (status, body)."""

    def call(self, verb: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Dispatch one memory-engine verb."""
        ...

    def healthz(self) -> tuple[int, dict[str, Any]]:
        """Return the backend health response."""
        ...


class InProcessClient:
    """Assemble an engine in this process and route verbs through it.

    ``configs`` are paths to JSON config layers stacked on top of the OFFLINE
    profile (nearest-wins), matching ``bootstrap/http_server/__main__``. The built
    :class:`Server` is held for the client's lifetime so writes persist across
    calls within the process.
    """

    def __init__(self, configs: list[str] | None = None) -> None:
        import server
        from profiles import OFFLINE, load_config

        spaces = server.default_spaces()
        layers: list[dict] = [OFFLINE]
        for path in configs or []:
            with open(path, "r", encoding="utf-8") as fh:
                layers.append(json.load(fh))
        config = load_config(layers, spaces)
        self._srv = server.build(config, spaces=spaces)

    @property
    def server(self):
        """The assembled :class:`bootstrap.server.Server` (for direct inspection)."""
        return self._srv

    def call(self, verb: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        from auth_middleware import authenticated
        from handler import dispatch

        from common.authentication.types import Credentials
        from common.errors import AuthenticationError

        # 进程内直连没有 HTTP header，故过一个空 Credentials。DEV 模式下得到
        # ROOT，与现状一致（CLI 一直是全权限的）；API_KEY 模式下会认证失败——
        # 这是**正确的**：没有凭据就不该有权限。要在 API_KEY 模式下用 CLI，
        # 走 HttpClient 带 --api-key。
        #
        # 认证失败转成 (401, body) 而非抛出：本方法的契约是返回状态码，
        # 与 HttpClient.call 一致。
        try:
            with authenticated(self._srv.authenticator, Credentials(), self._srv.audit):
                return dispatch(self._srv, verb, payload)
        except AuthenticationError as exc:
            return 401, {"error": type(exc).__name__, "message": str(exc)}

    def healthz(self) -> tuple[int, dict[str, Any]]:
        return 200, {"status": "ok", "profile": self._srv.config.profile}


class HttpClient:
    """Drive a running ``bootstrap`` server over HTTP (``POST /v1/<verb>``)."""

    def __init__(self, base_url: str, timeout: float = 30.0, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key

    def _request(self, method: str, path: str, body: dict | None) -> tuple[int, dict[str, Any]]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, _read_json(resp)
        except urllib.error.HTTPError as exc:
            # Domain errors come back as a JSON body with a non-2xx status.
            return exc.code, _read_json(exc)
        except urllib.error.URLError as exc:
            return 0, {"error": "ConnectionError", "message": str(exc.reason)}

    def call(self, verb: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self._request("POST", f"/v1/{verb}", payload)

    def healthz(self) -> tuple[int, dict[str, Any]]:
        return self._request("GET", "/healthz", None)


def _read_json(resp) -> dict[str, Any]:
    raw = resp.read()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {"error": "BadResponse", "message": raw.decode("utf-8", "replace")}


def make_client(
    server_url: str | None,
    configs: list[str] | None = None,
    api_key: str | None = None,
) -> EngineClient:
    """Pick a backend: HTTP when ``server_url`` is given, else in-process.

    ``api_key`` 缺省读环境变量 ``AGENT_MEMORY_API_KEY``——让 key 不必出现在
    shell history 与 ``ps`` 输出里。
    """
    if server_url:
        return HttpClient(server_url, api_key=api_key or os.environ.get("AGENT_MEMORY_API_KEY", ""))
    return InProcessClient(configs)
