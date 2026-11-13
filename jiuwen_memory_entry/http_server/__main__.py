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
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from openai import RateLimitError

from jiuwen_memory_entry.core.api_contract import (
    CONFIG_VERBS,
    invoke_api,
    invoke_configuration,
    is_known_verb,
)
from jiuwen_memory_entry.core.dev_security import with_local_dev_security
from jiuwen_memory_entry.core.error_response import error_response
from jiuwen_memory_entry.core.import_support import import_required, import_required_attr
from jiuwen_memory_entry.http_server.human_view import handle_human_request
from jiuwen_memory_entry.portal.requests import BROWSER_VERBS, invoke_browser_api
from jiuwen_memory_entry.portal.sessions import (
    BROWSER_HEADER,
    SESSION_HEADER,
    BrowserSessions,
    bootstrap,
    frame_headers,
    frame_origins,
)

# 共享应用核（server / profiles / handler / config_loader）住在 jiuwen_memory_entry/core；
# 加入 sys.path 后 flat-import 复用——本 surface 只做 HTTP 传输。
_CORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

logger = logging.getLogger("agent-memory.server")

load_layer = import_required_attr("config_loader", "load_layer")
_profiles_module = import_required("profiles")
OFFLINE = _profiles_module.OFFLINE
load_config = _profiles_module.load_config
Server = import_required_attr("server", "Server")
_auth_middleware = import_required("auth_middleware")
authenticated = _auth_middleware.authenticated
credentials_from_headers = _auth_middleware.credentials_from_headers
_api_module = import_required("jiuwen_memory.api")
Surface = _api_module.Surface
AgentMemoryError = _api_module.AgentMemoryError
AuthenticationError = _api_module.AuthenticationError
RateLimitedError = _api_module.RateLimitedError
ValidationError = _api_module.ValidationError

_StdThreadingHTTPServer = ThreadingHTTPServer

_MAX_BODY_BYTES = 4 * 1024 * 1024
_READ_TIMEOUT = 30
_MAX_CONCURRENT_REQUESTS = 256
_AUTH_MODE_ENV = "JIUWEN_MEMORY_HTTP_AUTH_MODE"
_AUTH_MODES = frozenset({"required", "dev"})


def _portal_write_scope(value, actor):
    fields = {"space", "agent", "session"}
    if (
        not isinstance(value, dict) or set(value) != fields
        or any(not isinstance(item, str) for item in value.values())
    ):
        raise ValidationError("invalid Portal write scope")
    return _api_module.Scope(org=actor.org, user=actor.user, **value)


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


def _server_port(value: str) -> int:
    """argparse 端口类型：拒绝越界值，避免 socket 层抛裸 OverflowError。"""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


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
        self.portal_enabled = False
        http_settings = config.settings.get("http", {})
        if not isinstance(http_settings, dict):
            raise ValidationError("http configuration must be an object")
        self.portal_frame_origins = frame_origins(http_settings.get("portal_frame_origins", ()))
        self.portal_sessions = BrowserSessions()

    @property
    def configuration(self):
        return getattr(self._runtime, "configuration", None)

    def close(self, *, wait=True):
        self.portal_sessions.close()
        super().close(wait=wait)

    @classmethod
    def build(cls, config, spaces=None, *, security_runtime=None):
        """Build the shared kernel and attach the HTTP security runtime."""
        from jiuwen_memory.api import (
            document_llm_instances,
            fixed_personal_owner,
            open_config_overlay,
        )

        memory_config = config.settings.get("memory_api")
        owner = fixed_personal_owner(memory_config) if security_runtime is None else None
        store = None
        if owner is not None:
            store = open_config_overlay(memory_config, config.config_paths)
        if store is not None:
            names = document_llm_instances(memory_config)

            def authorize(security, operation):
                if security.auth.actor != owner:
                    return False
                server.api.check_configuration_access(
                    owner, security=security, write=operation == "write"
                )
                return True

            server = cls.build_personal(
                config,
                store=store,
                llm_instances=names,
                authorize=authorize,
            )
            return server
        if security_runtime is None:
            server = super().build(config, spaces)
        else:
            server = super().build(config, spaces, security_runtime=security_runtime)
        server.portal_enabled = True
        return server

    @classmethod
    def build_personal(
        cls,
        config,
        *,
        llm_instances,
        authorize,
        store=None,
        url_allowed=None,
        locked_keys=frozenset(),
        security_runtime=None,
    ):
        """可信宿主在同一 Runtime 装配 Portal 和可选配置管理。"""
        from jiuwen_memory.api import (
            assemble_managed_server_runtime,
            open_config_overlay,
        )

        kwargs = {} if security_runtime is None else {"security_runtime": security_runtime}
        if store is None:
            store = open_config_overlay(config.settings.get("memory_api"), config.config_paths)
        if store is None:
            server = super().build(config, **kwargs)
            server.portal_enabled = True
            return server
        runtime = assemble_managed_server_runtime(
            config.settings.get("memory_api"),
            store=store,
            llm_instances=llm_instances,
            authorize=authorize,
            url_allowed=url_allowed,
            locked_keys=locked_keys,
            policies=config.policies or None,
            **kwargs,
        )
        try:
            server = cls(config, runtime, runtime.security_runtime)
            server.portal_enabled = True
            return server
        except BaseException as error:
            try:
                runtime.close(wait=True)
            except BaseException as cleanup_error:
                raise error from cleanup_error
            raise

    def handler_cls(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            timeout = _READ_TIMEOUT

            @contextmanager
            def human_primary_auth(self):
                runtime = srv.security_runtime
                if runtime is None:
                    raise AuthenticationError("primary authentication unavailable")
                peer = self.client_address[0] if self.client_address else ""
                with authenticated(
                    runtime.authenticator,
                    credentials_from_headers(self.headers, peer_address=peer),
                    audit=getattr(runtime, "audit", None) or srv.audit,
                    limiter=getattr(runtime, "rate_limiter", None),
                    workload_guard=getattr(runtime, "workload_guard", None),
                    surface=Surface.HTTP,
                ) as security:
                    yield security

            def handle_get(self) -> None:
                if self.path.startswith("/security/privacy") and handle_human_request(
                    self, srv.human_view, post=False
                ):
                    return
                request_id = uuid.uuid4().hex
                if self.path.rstrip("/") == "/portal" and srv.portal_enabled:
                    if not self._portal_origin_allowed(require_origin=False):
                        self._send_error("NotFound", request_id=request_id)
                        return
                    data = files("jiuwen_memory_entry.portal").joinpath("index.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    for name, value in frame_headers(srv.portal_frame_origins).items():
                        self.send_header(name, value)
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path.rstrip("/") == "/healthz":
                    self._send(
                        200,
                        {"status": "ok", "profile": srv.config.profile},
                        request_id=request_id,
                    )
                else:
                    self._send_error("NotFound", self.path, request_id=request_id)

            def handle_post(self) -> None:
                if self.path.startswith("/security/privacy") and handle_human_request(
                    self, srv.human_view, post=True, primary_auth=self.human_primary_auth
                ):
                    return
                request_id = uuid.uuid4().hex
                is_portal = self.path.startswith("/portal/api/")
                browser_request = self._uses_portal_session()
                if is_portal:
                    verb = self.path.removeprefix("/portal/api/").strip("/")
                    if verb != "bootstrap" and (verb != "launch" or browser_request):
                        self._send_error("NotFound", request_id=request_id)
                        return
                elif self.path.startswith("/v1/"):
                    verb = self.path.removeprefix("/v1/").strip("/")
                    if browser_request and verb not in BROWSER_VERBS:
                        self._send_error("NotFound", request_id=request_id)
                        return
                    if not is_known_verb(verb) and verb not in CONFIG_VERBS:
                        self._send_error("UnknownVerb", verb, request_id=request_id)
                        return
                else:
                    self._send_error("NotFound", self.path, request_id=request_id)
                    return
                portal_request = is_portal or browser_request
                if portal_request and (
                    not srv.portal_enabled or not self._portal_origin_allowed(require_origin=True)
                ):
                    self._send_error("NotFound", request_id=request_id)
                    return
                if portal_request or verb in CONFIG_VERBS:
                    if self.headers.get_content_type() != "application/json":
                        self._send_error("BadRequest", "JSON required", request_id=request_id)
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
                if is_portal and verb == "bootstrap":
                    if length > 512:
                        self._send_error("BadRequest", request_id=request_id)
                        return
                    try:
                        body, cookie = bootstrap(
                            srv.portal_sessions, json.loads(_read_body(self.rfile, length)),
                            srv.portal_frame_origins,
                        )
                    except (ValueError, TypeError):
                        body, cookie = None, None
                    if body is None:
                        self._send_error("AuthenticationError", request_id=request_id)
                    else:
                        self._send(200, body, request_id=request_id, cookie=cookie)
                    return
                credentials = credentials_from_headers(self.headers, peer_address=peer)
                write_scope = None
                if browser_request:
                    valid, access = srv.portal_sessions.lookup(
                        self.headers.get("Cookie"), token=self.headers.get(SESSION_HEADER),
                    )
                    if not valid:
                        self._send_error("AuthenticationError", request_id=request_id)
                        return
                    credentials, owner, write_scope = access
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
                        if browser_request and security.auth.actor != owner:
                            raise AuthenticationError("Portal session identity changed")
                        # 凭据和入口保护通过后才读取请求体，慢上传不能绕过认证占用内存。
                        raw = _read_body(self.rfile, length)
                        try:
                            payload = json.loads(raw) if raw else None
                        except (TypeError, ValueError) as json_error:
                            self._send_error(
                                "BadRequest",
                                f"invalid JSON: {json_error}",
                                request_id=security.request_id,
                            )
                            return
                        if is_portal:
                            if not isinstance(payload, dict) or payload.keys() - {"write_target"}:
                                raise ValidationError("invalid Portal launch fields")
                            owner = deepcopy(security.auth.actor)
                            if "write_target" in payload:
                                write_scope = _portal_write_scope(payload["write_target"], owner)
                            token = srv.portal_sessions.issue((credentials, owner, write_scope))
                            body = {
                                "portal_url": urlunsplit((
                                    "http", self.headers["Host"], "/portal/", "", f"access={token}",
                                )),
                            }
                        elif browser_request:
                            body = invoke_browser_api(
                                srv.api, verb, payload, configuration=srv.configuration,
                                security=security, write_scope=write_scope,
                            )
                        elif verb in CONFIG_VERBS:
                            body = invoke_configuration(
                                srv.configuration, verb, payload, security,
                            )
                        else:
                            body = invoke_api(srv.api, verb, payload, security)
                        self._send(200, body, request_id=security.request_id)
                except AuthenticationError:
                    self._send_error("AuthenticationError", request_id=request_id)
                except RateLimitedError:
                    self._send_error("RateLimitedError", request_id=request_id)
                except (AgentMemoryError, RateLimitError) as api_error:
                    self._send_error(type(api_error), api_error, request_id=context_request_id)
                except Exception as request_error:
                    logger.error(
                        "HTTP request failed request_id=%s error_type=%s",
                        context_request_id,
                        type(request_error).__name__,
                    )
                    self._send_error("InternalError", request_id=context_request_id)

            def _uses_portal_session(self) -> bool:
                return (
                    self.headers.get(BROWSER_HEADER) is not None
                    or self.headers.get(SESSION_HEADER) is not None
                    or (
                        not (self.headers.get("Authorization") or self.headers.get("X-API-Key"))
                        and srv.portal_sessions.has_cookie(self.headers.get("Cookie"))
                    )
                )

            def _portal_origin_allowed(self, *, require_origin: bool) -> bool:
                # 仅通过本机访问；限制 Host 可抵御浏览器 DNS 重绑定。
                raw_host = self.headers.get("Host", "")
                try:
                    host = urlsplit("http://" + raw_host)
                    if (
                        host.username
                        or host.password
                        or host.hostname
                        not in {
                            "localhost",
                            "127.0.0.1",
                            "::1",
                        }
                    ):
                        return False
                    if host.port != self.server.server_address[1]:
                        return False
                    origin = self.headers.get("Origin")
                    if not origin:
                        return not require_origin
                    return origin == "http://" + raw_host
                except ValueError:
                    return False

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
                cookie: str | None = None,
            ) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Request-ID", request_id)
                path = self.path.rstrip("/")
                portal_route = path == "/portal" or self.path.startswith("/portal/api/")
                config_route = path.removeprefix("/v1/") in CONFIG_VERBS
                if portal_route or self._uses_portal_session() or config_route:
                    self.send_header("Cache-Control", "no-store")
                if cookie is not None:
                    self.send_header("Set-Cookie", cookie)
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
        # 同步宿主无事件循环：起 daemon 线程自持 loop 跑看门狗
        self._runtime.start()
        httpd = None
        try:
            self._check_binding(host)
            self.restore_dreaming()
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


def build_parser() -> argparse.ArgumentParser:
    """构造 HTTP 入口的参数解析器。

    独立于 :func:`main`，供测试直接检查 ``prog`` 与参数集合，无需真实解析
    （``--help`` / 非法参数会让 argparse 抛 ``SystemExit``）。
    """
    parser = argparse.ArgumentParser(prog="agent-memory-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_server_port, default=8137)
    parser.add_argument(
        "--auth-mode",
        choices=sorted(_AUTH_MODES),
        default=os.getenv(_AUTH_MODE_ENV, "required"),
        help=f"HTTP authentication mode (default: ${_AUTH_MODE_ENV} or required)",
    )
    parser.add_argument("config", nargs="*", help="JSON/YAML config layers stacked on OFFLINE")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.auth_mode not in _AUTH_MODES:
        parser.error(f"invalid {_AUTH_MODE_ENV}: {args.auth_mode!r}")
    layers = [OFFLINE]
    try:
        for path in args.config:
            layers.append(load_layer(path))
        config = load_config(layers)
        from dataclasses import replace

        config = replace(config, config_paths=tuple(args.config))
        if args.auth_mode == "dev":
            http_settings = config.settings.get("http", {})
            if not isinstance(http_settings, dict):
                raise ValidationError("http configuration must be an object")
            if "dev_identities" in http_settings and http_settings["dev_identities"] is None:
                raise ValidationError("http.dev_identities must not be null")
            config = with_local_dev_security(config, identities=http_settings.get("dev_identities"))
            logger.warning(
                "development authentication is enabled; identities are for local testing only "
                "and this mode must not be used in production"
            )
        srv = HttpServer.build(config)
        srv.serve(args.host, args.port)
    except (ValidationError, OSError) as exc:
        logger.error("HTTP server refused to start: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
