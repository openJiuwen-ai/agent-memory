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
import json
import logging
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from typing import Any

from jiuwen_memory_entry.core.api_contract import invoke_api, is_known_verb
from jiuwen_memory_entry.core.dev_security import with_local_dev_security
from jiuwen_memory_entry.core.error_response import error_response

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
_StdThreadingHTTPServer = ThreadingHTTPServer

_MAX_BODY_BYTES = 4 * 1024 * 1024
_READ_TIMEOUT = 30
_MAX_CONCURRENT_REQUESTS = 256

_AUTH_MODE_ENV = "JIUWEN_MEMORY_HTTP_AUTH_MODE"
_AUTH_MODES = frozenset({"required", "dev"})


def _parse_content_length(headers, *, max_body_bytes: int = _MAX_BODY_BYTES) -> tuple[int, int]:
    """只校验 Content-Length；返回 ``(HTTP status, length)``。"""
    raw_length = headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        return 400, 0
    if length < 0:
        return 400, 0
    if length > max_body_bytes:
        return 413, 0
    return 200, length


def _read_body(rfile, length: int) -> bytes:
    """按已校验长度读取请求体。"""
    return rfile.read(length) if length else b""


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """限制并发处理线程，避免慢连接无界耗尽进程资源。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(_MAX_CONCURRENT_REQUESTS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                self._send_503(request)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        thread = threading.Thread(
            target=self._process_and_release,
            args=(request, client_address),
            daemon=self.daemon_threads,
        )
        thread.start()

    def _process_and_release(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            self._slots.release()

    @staticmethod
    def _send_503(request) -> None:
        body = b'{"error":"ServiceUnavailable","message":"too many connections"}'
        crlf = b"\r\n"
        request.sendall(
            b"HTTP/1.0 503 Service Unavailable"
            + crlf
            + b"Content-Length: "
            + str(len(body)).encode()
            + crlf
            + b"Content-Type: application/json"
            + crlf
            + crlf
            + body
        )


class HttpServer(Server):
    """The HTTP/socket surface over same-named ``MemoryAPI`` calls."""

    max_body_bytes = _MAX_BODY_BYTES

    def __init__(self, config, runtime, security_runtime=None) -> None:
        super().__init__(config, runtime, security_runtime)

    @classmethod
    def build(cls, config, spaces=None, *, security_runtime=None):
        """Build the shared kernel and attach the HTTP security runtime."""
        if security_runtime is None:
            return super().build(config, spaces)
        return super().build(config, spaces, security_runtime=security_runtime)

    def handler_cls(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            timeout = _READ_TIMEOUT

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
                length_status, length = _parse_content_length(
                    self.headers, max_body_bytes=srv.max_body_bytes
                )
                if length_status == 400:
                    self._send_error("BadRequest", "invalid Content-Length", request_id=request_id)
                    return
                if length_status == 413:
                    self._send_error("PayloadTooLarge", request_id=request_id)
                    return
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
                        audit=getattr(runtime, "audit", None) or srv.audit,
                        limiter=getattr(runtime, "rate_limiter", None),
                        workload_guard=getattr(runtime, "workload_guard", None),
                        surface=Surface.HTTP,
                        request_id=request_id,
                    ) as security:
                        context_request_id = security.request_id
                        # 凭据和入口保护通过后才读取请求体，慢上传不能绕过认证占用内存。
                        raw = _read_body(self.rfile, length)
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

    # PR1 兼容名；既有嵌入式调用与资源保护测试仍通过该入口取 Handler。
    def _handler_cls(self):
        return self.handler_cls()

    def _check_binding(self, host: str) -> None:
        runtime = self.security_runtime
        if runtime is None:
            return
        authenticator = runtime.authenticator
        requirement = getattr(authenticator, "requires_loopback_binding", None)
        requires_loopback = bool(requirement()) if callable(requirement) else False
        policy = getattr(runtime, "binding_policy", None)
        if policy is None:
            raise ValidationError("security runtime is missing a binding policy")
        policy.check(host, requires_loopback=requires_loopback)

    def serve(self, host: str, port: int) -> None:
        httpd = None
        try:
            self._check_binding(host)
            # 上游嵌入测试会替换标准 server factory；正常运行始终使用有界实现。
            server_cls = (
                ThreadingHTTPServer
                if ThreadingHTTPServer is not _StdThreadingHTTPServer
                else _BoundedThreadingHTTPServer
            )
            httpd = server_cls((host, port), self.handler_cls())
            httpd.daemon_threads = True
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
    layers = [OFFLINE]
    for path in args.config:
        layers.append(load_layer(path))
    config = load_config(layers)
    if args.auth_mode == "dev":
        config = with_local_dev_security(config)
        logger.warning(
            "development authentication is enabled; credentials are ignored and this mode "
            "must not be used in production"
        )
    srv = HttpServer.build(config)  # 基类 build → HttpServer 实例
    try:
        srv.serve(args.host, args.port)
    except ValidationError as exc:
        logger.error("HTTP server refused to start: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
