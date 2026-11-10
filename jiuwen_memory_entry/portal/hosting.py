# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A local browser surface borrowing a host-owned SDK Runtime and trusted identity."""

from __future__ import annotations

import json
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from threading import BoundedSemaphore, Lock, RLock, Thread
from urllib.parse import urlsplit
from weakref import ref

from jiuwen_memory.api import (
    AgentMemoryError,
    PermissionDeniedError,
    Surface,
    ValidationError,
    new_request_context,
)
from jiuwen_memory_entry.portal.requests import BROWSER_VERBS, invoke_browser_api
from jiuwen_memory_entry.portal.sessions import (
    SESSION_HEADER,
    BrowserSessions,
    bootstrap,
    frame_headers,
    frame_origins,
)


class _BoundedServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, *args):
        self.slots = BoundedSemaphore(16)
        super().__init__(*args)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class PortalHandle:
    """Owns only web resources. Closing never closes the borrowed memory Runtime."""

    def __init__(self, runtime, security_provider, *, port=0, allowed_frame_origins=()):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValidationError("Portal port must be an integer from 0 to 65535")
        self.frame_origins = frame_origins(allowed_frame_origins)
        self.owner = deepcopy(security_provider().auth.actor)
        self.runtime = runtime
        self.provider = security_provider
        self.sessions = BrowserSessions()
        self.close_lock = Lock()
        self.closed = False
        handle = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10

            def allowed(self, *, origin=False):
                try:
                    host = urlsplit("http://" + self.headers.get("Host", ""))
                    if host.hostname not in {"127.0.0.1", "localhost"}:
                        return False
                    if any((host.username, host.password, host.path, host.query, host.fragment)):
                        return False
                    if host.port != self.server.server_port:
                        return False
                except ValueError:
                    return False
                if origin and self.headers.get("Origin") != "http://" + self.headers["Host"]:
                    return False
                return True

            def respond(self, status, payload, *, cookie=None, html=False):
                data = payload if html else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header(
                    "Content-Type", "text/html; charset=utf-8" if html else "application/json"
                )
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for name, value in frame_headers(handle.frame_origins if html else ()).items():
                    self.send_header(name, value)
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

            def handle_get(self):
                if not self.allowed() or self.path.rstrip("/") != "/portal":
                    self.respond(404, {"error": "NotFound"})
                    return
                self.respond(
                    200,
                    files("jiuwen_memory_entry.portal").joinpath("index.html").read_bytes(),
                    html=True,
                )

            def handle_post(self):
                try:
                    is_bootstrap = self.path.rstrip("/") == "/portal/api/bootstrap"
                    verb = self.path.removeprefix("/v1/").strip("/")
                    if not self.allowed(origin=True) or not (
                        is_bootstrap or (self.path.startswith("/v1/") and verb in BROWSER_VERBS)
                    ):
                        self.respond(404, {"error": "NotFound"})
                        return
                    if self.headers.get("Transfer-Encoding"):
                        raise ValidationError("transfer encoding is not supported")
                    if self.headers.get_content_type() != "application/json":
                        raise ValidationError("JSON required")
                    raw_length = self.headers.get("Content-Length", "")
                    if not raw_length.isdecimal() or not 0 < int(raw_length) <= 1024 * 1024:
                        raise ValidationError("invalid request length")
                    payload = json.loads(self.rfile.read(int(raw_length)))
                    if not isinstance(payload, dict):
                        raise ValidationError("payload must be an object")
                    if is_bootstrap:
                        body, cookie = bootstrap(handle.sessions, payload, handle.frame_origins)
                        if body is None:
                            self.respond(401, {"error": "AuthenticationError"})
                        else:
                            self.respond(200, body, cookie=cookie)
                        return
                    valid, write_scope = handle.sessions.lookup(
                        self.headers.get("Cookie"), token=self.headers.get(SESSION_HEADER),
                    )
                    if not valid:
                        self.respond(
                            401,
                            {"error": "AuthenticationError", "message": "请从宿主重新打开记忆管理"},
                        )
                        return
                    supplied = handle.provider()
                    if supplied.auth.actor != handle.owner:
                        raise PermissionDeniedError(message="personal instance identity changed")
                    security = new_request_context(
                        supplied.auth, surface=Surface.HTTP, peer=self.client_address[0]
                    )
                    self.respond(200, invoke_browser_api(
                        handle.runtime.api, verb, payload, security=security,
                        write_scope=write_scope,
                        configuration=getattr(handle.runtime, "configuration", None),
                    ))
                except PermissionDeniedError:
                    self.respond(403, {"error": "PermissionDeniedError"})
                except AgentMemoryError as error:
                    status = {
                        "NotFoundError": 404,
                        "ConflictError": 409,
                        "AuthenticationError": 401,
                        "RateLimitedError": 429,
                        "BackendError": 503,
                    }.get(type(error).__name__, 400)
                    self.respond(
                        status,
                        {
                            "error": type(error).__name__,
                            "message": "请求失败，请检查权限、输入或实例状态",
                        },
                    )
                except ValueError:
                    self.respond(
                        400,
                        {
                            "error": "RequestFailed",
                            "message": "请求失败，请检查权限、输入或实例状态",
                        },
                    )
                except Exception:
                    self.respond(500, {"error": "InternalError"})

            def log_message(self, *_args):
                pass

        setattr(Handler, "do_GET", Handler.handle_get)
        setattr(Handler, "do_POST", Handler.handle_post)
        self.server = _BoundedServer(("127.0.0.1", port), Handler)
        self.address = f"http://127.0.0.1:{self.server.server_port}/portal/"
        self.thread = Thread(target=self.server.serve_forever, daemon=True, name="memory-portal")
        try:
            self.thread.start()
        except BaseException:
            self.server.server_close()
            raise

    def launch_url(self, write_scope=None):
        if self.closed:
            return None
        return self.address + "#access=" + self.sessions.issue(deepcopy(write_scope))

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
            self.sessions.close()
            with _registry_lock:
                if _handles.get(id(self.runtime), lambda: None)() is self:
                    del _handles[id(self.runtime)]


_registry_lock = RLock()
_handles = {}


def start_portal(runtime, *, security_provider, port=0, allowed_frame_origins=()):
    """Attach to an existing instance; the host must call once and own the handle."""
    origins = frame_origins(allowed_frame_origins)
    with _registry_lock:
        existing = _handles.get(id(runtime), lambda: None)()
        if existing is not None and not existing.closed:
            if existing.owner != security_provider().auth.actor:
                raise PermissionDeniedError(message="Runtime is already bound to another identity")
            if port and port != existing.server.server_port:
                raise ValidationError("Runtime already has a Portal on another port")
            if origins != existing.frame_origins:
                raise ValidationError("Runtime already has a different Portal framing policy")
            return existing
        handle = PortalHandle(
            runtime, security_provider, port=port, allowed_frame_origins=origins,
        )
        _handles[id(runtime)] = ref(handle)
        return handle
