# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CLI clients share MemoryAPI JSON parameters and unwrapped return values."""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import sys
import urllib.error
import urllib.request
import uuid
from types import SimpleNamespace
from typing import Any, Protocol

from openai import RateLimitError

from jiuwen_memory.api import AgentMemoryError, Credentials, Surface
from jiuwen_memory_entry.core.api_contract import invoke_api, is_known_verb
from jiuwen_memory_entry.core.auth_middleware import authenticated
from jiuwen_memory_entry.core.dev_security import with_local_dev_security
from jiuwen_memory_entry.core.error_response import error_response

_CORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

logger = logging.getLogger("agent-memory.cli")

DEFAULT_HTTP_TIMEOUT = 300.0


def validate_timeout(timeout: float) -> float:
    """Return one finite positive timeout in seconds; reject any other value.

    CLI 的 ``--timeout`` / ``AGENT_MEMORY_TIMEOUT`` 与程序化 ``make_client`` /
    ``HttpClient`` 共用这一条校验，避免只有命令行入口挡非法值、库调用仍把
    ``0`` / ``nan`` / 负数透给 ``urllib``。
    """
    try:
        value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"timeout must be a finite positive number: {timeout!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"timeout must be a finite positive number: {timeout!r}")
    return value


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
        except (AgentMemoryError, RateLimitError) as exc:
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

    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
        api_key: str | None = None,
    ) -> None:
        """Configure the endpoint and credentials without changing API payloads."""
        self.base_url = base_url.rstrip("/")
        self.timeout = validate_timeout(timeout)
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
            return self._send(request)
        except (TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            # urllib 并不把 getresponse()/响应体读取阶段的所有传输失败包装为
            # URLError。超时、对端断开、RST 和不完整 HTTP 响应必须在此统一映射，
            # 避免异常穿透 call() 的二元组契约并被 CLI 错误地归为参数失败。
            error_type = type(exc).__name__
            message = f"HTTP transport failed (error={error_type})"
            if isinstance(exc, TimeoutError):
                message = (
                    "HTTP socket operation timed out "
                    f"(error={error_type}, timeout={self.timeout:g}s)"
                )
            return 0, {
                "error": "ConnectionError",
                "message": message,
            }

    def _send(self, request: urllib.request.Request) -> tuple[int, Any]:
        """Send one request, mapping HTTP and transport failures to the tuple contract."""
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return _read_json(response, transport_status=response.status)
        except urllib.error.HTTPError as exc:
            return _read_json(exc, transport_status=exc.code)
        except urllib.error.URLError as exc:
            return 0, {"error": "ConnectionError", "message": str(exc.reason)}


def _read_json(response, *, transport_status: int) -> tuple[int, Any]:
    """Decode the body; a non-JSON body is a protocol-level failure, never a success.

    HTTP 2xx 但响应体不是合法 JSON 时，不能沿用 2xx 状态——``_write`` 只看状态
    决定退出码与输出流，200 会让 CLI 报成功（退出码 0）却只打出 BadResponse
    错误体。此类故障映射为 502（协议层错误）；HTTP 错误响应本身非 JSON 时保留
    原状态码（已是失败，信息更有用）。
    """
    raw = response.read()
    try:
        return transport_status, json.loads(raw)
    except ValueError:
        status = transport_status if transport_status >= 400 else 502
        return status, {"error": "BadResponse", "message": "server returned invalid JSON"}


def make_client(
    server_url: str | None,
    configs: list[str] | None = None,
    *,
    auth_mode: str = "required",
    timeout: float = DEFAULT_HTTP_TIMEOUT,
) -> EngineClient:
    """Select HTTP or in-process execution; dev authentication is explicit and local."""
    if auth_mode not in {"required", "dev"}:
        raise ValueError(f"unknown authentication mode: {auth_mode}")
    timeout = validate_timeout(timeout)
    if server_url:
        return HttpClient(server_url, timeout=timeout)
    dev_authentication = auth_mode == "dev"
    if dev_authentication:
        logger.warning("development authentication is enabled for local CLI testing")
    return InProcessClient(configs, dev_authentication=dev_authentication)
