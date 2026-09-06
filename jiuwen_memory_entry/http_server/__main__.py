# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP surface — a direct JSON transport over :class:`MemoryAPI`.

``HttpServer`` extends the base :class:`Server` for runtime assembly and adds
``POST /v1/<MemoryAPI method>`` plus ``GET /healthz``. Request fields and return
values are mechanically converted from the public API contract; this surface
does not use the legacy shared dispatch envelope. CLI uses the same API contract.

One assembled runtime is held for the server lifetime so state persists across
requests. Authentication supplies the sole non-JSON API argument, ``security``.

通过启动脚本运行，以便把仓库根与 ``jiuwen_memory_entry/core`` 放入 ``PYTHONPATH``::

    scripts/run-server.sh --auth-mode dev --port 8137
    scripts/run-server.sh [--auth-mode required|dev] [--host H] [--port P] [config.json ...]
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from typing import Any

from jiuwen_memory_entry.core.api_contract import invoke_api, is_known_verb
from jiuwen_memory_entry.core.error_response import error_response
from jiuwen_memory_entry.http_server.dev_security import build_dev_security_runtime

# 共享应用核（server / profiles / handler / config_loader）住在 jiuwen_memory_entry/core；
# 加入 sys.path 后 flat-import 复用——本 surface 只做 HTTP 传输。
_CORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

load_layer = import_module("config_loader").load_layer
_profiles_module = import_module("profiles")
OFFLINE = _profiles_module.OFFLINE
load_config = _profiles_module.load_config
Server = import_module("server").Server
authenticated = import_module("auth_middleware").authenticated
credentials_from_headers = import_module("auth_middleware").credentials_from_headers
_api_module = import_module("jiuwen_memory.api")
Surface = _api_module.Surface
AgentMemoryError = _api_module.AgentMemoryError
AuthenticationError = _api_module.AuthenticationError
RateLimitedError = _api_module.RateLimitedError
ValidationError = _api_module.ValidationError

logger = logging.getLogger("agent-memory.server")

_AUTH_MODE_ENV = "JIUWEN_MEMORY_HTTP_AUTH_MODE"
_ALLOW_DEV_NON_LOOPBACK_ENV = "JIUWEN_MEMORY_HTTP_ALLOW_DEV_AUTH_NON_LOOPBACK"
_AUTH_MODES = frozenset({"required", "dev"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


def _read_env_flag(name: str) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise ValidationError(f"environment variable {name} must be a boolean value")


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class HttpServer(Server):
    """The HTTP/socket surface over same-named ``MemoryAPI`` calls."""

    max_body_bytes = 1024 * 1024

    def __init__(self, config, kernel, *, security_runtime=None) -> None:
        super().__init__(config, kernel)
        self.security_runtime = security_runtime

    @classmethod
    def build(cls, config, spaces=None, *, security_runtime=None):
        """Build the shared kernel and attach the HTTP security runtime."""
        server = super().build(config, spaces)
        server.security_runtime = security_runtime
        return server

    def handler_cls(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def handle_get(self) -> None:
                request_id = uuid.uuid4().hex
                if self.path.rstrip("/") == "/healthz":
                    self._send(
                        200,
                        {"status": "ok", "profile": srv.config.profile},
                        request_id=request_id,
                    )
                else:
                    self._send_error("NotFound", self.path, request_id=request_id)

            def handle_post(self) -> None:
                request_id = uuid.uuid4().hex
                if not self.path.startswith("/v1/"):
                    self._send_error("NotFound", self.path, request_id=request_id)
                    return
                prefix_len = len("/v1/")
                verb = self.path[prefix_len:].strip("/")
                if not is_known_verb(verb):
                    self._send_error("UnknownVerb", verb, request_id=request_id)
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    self._send_error("BadRequest", "invalid Content-Length", request_id=request_id)
                    return
                if length < 0 or length > srv.max_body_bytes:
                    self._send_error("PayloadTooLarge", request_id=request_id)
                    return
                raw = self.rfile.read(length) if length else b""

                runtime = srv.security_runtime
                if runtime is None:
                    self._send_error("SecurityUnavailable", request_id=request_id)
                    return

                peer = self.client_address[0] if self.client_address else ""
                credentials = credentials_from_headers(self.headers, peer_address=peer)
                context_request_id = request_id
                try:
                    with authenticated(
                        runtime.authenticator,
                        credentials,
                        audit=getattr(runtime, "audit", None),
                        limiter=getattr(runtime, "rate_limiter", None),
                        workload_guard=getattr(runtime, "workload_guard", None),
                        surface=Surface.HTTP,
                        request_id=request_id,
                    ) as security:
                        context_request_id = security.request_id
                        try:
                            payload = json.loads(raw) if raw else None
                        except (TypeError, ValueError) as exc:
                            self._send_error(
                                "BadRequest", f"invalid JSON: {exc}", request_id=security.request_id
                            )
                            return
                        body = invoke_api(srv.api, verb, payload, security)
                        self._send(200, body, request_id=security.request_id)
                except AuthenticationError:
                    self._send_error("AuthenticationError", request_id=request_id)
                except RateLimitedError:
                    self._send_error("RateLimitedError", request_id=request_id)
                except AgentMemoryError as exc:
                    self._send_error(type(exc), exc, request_id=context_request_id)
                except Exception as exc:
                    logger.error(
                        "HTTP request failed request_id=%s error_type=%s",
                        context_request_id,
                        type(exc).__name__,
                    )
                    self._send_error("InternalError", request_id=context_request_id)

            def handle_unsupported(self) -> None:
                request_id = uuid.uuid4().hex
                self._send_error("MethodNotAllowed", self.command, request_id=request_id)

            def send_error(self, code, message=None, explain=None):  # noqa: ANN001
                if code == 501:
                    self.handle_unsupported()
                    return
                super().send_error(code, message, explain)

            def log_message(  # pyright: ignore[reportIncompatibleMethodOverride]
                self, message_format, *args
            ) -> None:  # quiet by default
                pass

            def _send_error(
                self,
                error: object,
                detail: object = "",
                *,
                request_id: str,
            ) -> None:
                status, body, retry_after = error_response(error, detail)
                body["request_id"] = request_id
                self._send(status, body, request_id=request_id, retry_after=retry_after)

            def _send(
                self,
                status: int,
                body: Any,
                *,
                request_id: str,
                retry_after: int | None = None,
            ) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Request-ID", request_id)
                if retry_after is not None:
                    self.send_header("Retry-After", str(retry_after))
                self.end_headers()
                self.wfile.write(data)

        setattr(Handler, "do_GET", Handler.handle_get)
        setattr(Handler, "do_POST", Handler.handle_post)
        for method in ("PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE"):
            setattr(Handler, f"do_{method}", Handler.handle_unsupported)
        return Handler

    def _check_binding(self, host: str, *, allow_dev_non_loopback: bool) -> None:
        runtime = self.security_runtime
        if runtime is None:
            return
        authenticator = runtime.authenticator
        requirement = getattr(authenticator, "requires_loopback_binding", None)
        requires_loopback = bool(requirement()) if callable(requirement) else False
        policy = getattr(runtime, "binding_policy", None)
        if policy is not None:
            policy.check(host, requires_loopback=requires_loopback)
            return
        if not requires_loopback or _is_loopback_host(host):
            return
        mode_method = getattr(authenticator, "mode", None)
        mode = mode_method() if callable(mode_method) else ""
        if mode == "dev" and allow_dev_non_loopback:
            logger.warning(
                "development authentication is listening on non-loopback host %s; "
                "the deployment boundary must prevent remote access",
                host,
            )
            return
        if mode == "dev":
            raise ValidationError(
                "development authentication may bind only to a loopback host; "
                f"set {_ALLOW_DEV_NON_LOOPBACK_ENV}=true only inside an isolated container"
            )
        raise ValidationError(
            f"authenticator mode {mode!r} requires a loopback host; "
            "use an authenticator that supports non-loopback binding"
        )

    def serve(self, host: str, port: int, *, allow_dev_non_loopback: bool = False) -> None:
        httpd = None
        try:
            self._check_binding(host, allow_dev_non_loopback=allow_dev_non_loopback)
            httpd = ThreadingHTTPServer((host, port), self.handler_cls())
            logger.info(
                "agent-memory server (profile=%s) on http://%s:%s",
                self.config.profile,
                host,
                port,
            )
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("agent-memory server stopped")
        finally:
            if httpd is not None:
                httpd.server_close()
            self.close(wait=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="agent-memory-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument(
        "--auth-mode",
        choices=sorted(_AUTH_MODES),
        default=os.getenv(_AUTH_MODE_ENV, "required"),
        help=f"HTTP authentication mode (default: ${_AUTH_MODE_ENV} or required)",
    )
    parser.add_argument("config", nargs="*", help="JSON/YAML config layers stacked on OFFLINE")
    args = parser.parse_args(argv)
    if args.auth_mode not in _AUTH_MODES:
        parser.error(f"invalid {_AUTH_MODE_ENV}: {args.auth_mode!r}")
    try:
        allow_dev_non_loopback = _read_env_flag(_ALLOW_DEV_NON_LOOPBACK_ENV)
    except ValidationError as exc:
        parser.error(str(exc))

    layers = [OFFLINE]
    for path in args.config:
        layers.append(load_layer(path))
    security_runtime = None
    if args.auth_mode == "dev":
        security_runtime = build_dev_security_runtime()
        logger.warning(
            "development authentication is enabled; credentials are ignored and this mode "
            "must not be used in production"
        )
    srv = HttpServer.build(
        load_config(layers), security_runtime=security_runtime
    )  # 基类 build → HttpServer 实例
    try:
        srv.serve(
            args.host,
            args.port,
            allow_dev_non_loopback=allow_dev_non_loopback,
        )
    except ValidationError as exc:
        logger.error("HTTP server refused to start: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
