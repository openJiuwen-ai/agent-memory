# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP surface — a stdlib server subclassing :class:`~server.Server`.

``HttpServer`` extends the base :class:`Server` (kernel + shared dispatch) and
adds a socket: ``POST /v1/<verb>`` with a JSON body, ``GET /healthz``. The
:class:`HttpClient` in ``jiuwen_memory_entry/cli/client.py`` speaks exactly this protocol,
and one assembled kernel is held for the server's lifetime so state persists
across requests.
The strict HTTP DTO body keeps authenticated actor separate from target and business fields.

通过启动脚本运行，以便把仓库根与 ``jiuwen_memory_entry/core`` 放入 ``PYTHONPATH``::

    scripts/run-server.sh --port 8137
    scripts/run-server.sh [--host H] [--port P] [config.json ...]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module

# Strict DTO parsing lives beside this HTTP surface.
from jiuwen_memory_entry.http_server.dto import is_known_verb, parse_request

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
safe_error_message = _api_module.safe_error_message
AgentMemoryError = _api_module.AgentMemoryError
AuthenticationError = _api_module.AuthenticationError
RateLimitedError = _api_module.RateLimitedError
ValidationError = _api_module.ValidationError

logger = logging.getLogger("agent-memory.server")

_RETRY_AFTER_SECONDS = 1
_HTTP_ERROR_POLICIES = {
    "BadRequest": (400, False, None),
    "ValidationError": (400, False, None),
    "PolicyError": (400, False, None),
    "AuthenticationError": (401, False, "authentication failed"),
    "PermissionDeniedError": (403, False, "permission denied"),
    "NotFound": (404, False, "resource not found"),
    "NotFoundError": (404, False, "resource not found"),
    "UnknownVerb": (404, False, "resource not found"),
    "MethodNotAllowed": (405, False, "method not allowed"),
    "ConflictError": (409, False, "resource conflict"),
    "PartialFailureError": (409, False, None),
    "RateLimitedError": (429, True, "too many requests"),
    "BackendError": (503, True, "service temporarily unavailable"),
    "HealthCheckError": (503, True, "service temporarily unavailable"),
    "SecurityUnavailable": (503, False, "HTTP authentication is not configured"),
    "PayloadTooLarge": (413, False, "request body is too large"),
    "InternalError": (500, False, "internal server error"),
    "UnsupportedStorageCapabilityError": (400, False, None),
    "StorageRetrievalError": (400, False, None),
}
_PARTIAL_FAILURE_FIELDS = ("completed", "failed", "retry_action")
_UNSET = object()


def _error_name(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, type):
        return value.__name__
    return type(value).__name__


def _http_error(error: object, detail: object = "") -> tuple[int, dict[str, object], int | None]:
    """Translate an internal error name into the stable HTTP error envelope."""
    name = _error_name(error)
    policy = _HTTP_ERROR_POLICIES.get(name)
    if policy is None:
        if isinstance(error, AgentMemoryError) or (
            isinstance(error, type) and issubclass(error, AgentMemoryError)
        ):
            status, retryable, fixed_message = 400, False, None
        else:
            name = "InternalError"
            status, retryable, fixed_message = _HTTP_ERROR_POLICIES[name]
    else:
        status, retryable, fixed_message = policy
    if fixed_message is None:
        message = safe_error_message(Exception(str(detail)))
    else:
        message = fixed_message
    body: dict[str, object] = {
        "error": name or "InternalError",
        "message": message,
        "retryable": retryable,
    }
    if name == "PartialFailureError":
        for field in _PARTIAL_FAILURE_FIELDS:
            value = getattr(detail, field, _UNSET)
            if value is not _UNSET:
                body[field] = value
    retry_after = _RETRY_AFTER_SECONDS if retryable and status == 429 else None
    return status, body, retry_after


class HttpServer(Server):
    """The HTTP/socket surface over the shared kernel dispatch."""

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
                    self._send_error(
                        "BadRequest", "invalid Content-Length", request_id=request_id
                    )
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
                        request = parse_request(verb, payload)
                        dispatch_request = request.to_dispatch_request(
                            actor=security.actor,
                            request_id=security.request_id,
                            security=security,
                        )
                        status, body = srv.dispatch(dispatch_request)
                        if status >= 400:
                            error_status, error_body, retry_after = _http_error(
                                body.get("error"), body.get("message", "")
                            )
                            for field in _PARTIAL_FAILURE_FIELDS:
                                if field in body:
                                    error_body[field] = body[field]
                            self._send(
                                error_status,
                                error_body,
                                request_id=security.request_id,
                                retry_after=retry_after,
                            )
                        else:
                            self._send(status, body, request_id=security.request_id)
                except AuthenticationError:
                    self._send_error("AuthenticationError", request_id=request_id)
                except RateLimitedError:
                    self._send_error("RateLimitedError", request_id=request_id)
                except ValidationError as exc:
                    self._send_error("ValidationError", exc, request_id=context_request_id)
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
                status, body, retry_after = _http_error(error, detail)
                self._send(status, body, request_id=request_id, retry_after=retry_after)

            def _send(
                self,
                status: int,
                body: dict,
                *,
                request_id: str,
                retry_after: int | None = None,
            ) -> None:
                body = dict(body)
                body["request_id"] = request_id
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

    def serve(self, host: str, port: int) -> None:
        # 同步宿主无事件循环：起 daemon 线程自持 loop 跑看门狗（F07 §12.10）。
        self._runtime.start()
        httpd = ThreadingHTTPServer((host, port), self.handler_cls())
        logger.info(
            "agent-memory server (profile=%s) on http://%s:%s",
            self.config.profile,
            host,
            port,
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("agent-memory server stopped")
        finally:
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
    parser.add_argument("config", nargs="*", help="JSON/YAML config layers stacked on OFFLINE")
    args = parser.parse_args(argv)

    layers = [OFFLINE]
    for path in args.config:
        layers.append(load_layer(path))
    srv = HttpServer.build(load_config(layers))  # 基类 build → HttpServer 实例
    srv.serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
