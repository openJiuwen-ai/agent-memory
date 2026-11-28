# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A local browser surface borrowing a host-owned SDK Runtime and trusted identity."""

from __future__ import annotations

import json
import os
import secrets
import socket
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from jiuwen_memory_entry.core.api_contract import CONFIG_VERBS
from jiuwen_memory_entry.portal.requests import (
    BROWSER_VERBS,
    invoke_browser_api,
    portal_experience_space,
)
from jiuwen_memory_entry.portal.sessions import (
    SESSION_HEADER,
    BrowserSessions,
    bootstrap,
)
from jiuwen_memory_entry.portal.static import serve_portal_file


def new_provider_key() -> str:
    """Return a per-Provider handle that cannot merge distinct credential sources."""
    return secrets.token_urlsafe(32)


class _BoundedServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, *args):
        self.slots = BoundedSemaphore(16)
        super().__init__(*args)

    def server_bind(self):
        if os.name == "nt":
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

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


def runtime_request(
    runtime, auth, verb, payload, write_scope, *, peer="127.0.0.1", experience_space=None,
):
    security = new_request_context(auth, surface=Surface.HTTP, peer=peer)
    return invoke_browser_api(
        runtime.api, verb, payload, security=security, write_scope=write_scope,
        configuration=getattr(runtime, "configuration", None), experience_space=experience_space,
    )


def request_error(error):
    if isinstance(error, PermissionDeniedError):
        return 403, {"error": "PermissionDeniedError"}
    if isinstance(error, AgentMemoryError):
        kind = type(error).__name__
        status = {
            "NotFoundError": 404,
            "ConflictError": 409,
            "AuthenticationError": 401,
            "RateLimitedError": 429,
            "BackendError": 503,
        }.get(kind, 400)
    elif isinstance(error, ValueError):
        status, kind = 400, "RequestFailed"
    else:
        return 500, {"error": "InternalError"}
    return status, {"error": kind, "message": "请求失败，请检查权限、输入或实例状态"}


class _PortalServer:
    def __init__(self, dispatch, *, port, shared=False):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValidationError("Portal port must be an integer from 0 to 65535")
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

            def respond(self, status, payload, *, cookie=None):
                data = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

            def handle_get(self):
                path = self.path.partition("?")[0] if shared else self.path
                if not self.allowed() or not serve_portal_file(self, path):
                    self.respond(404, {"error": "NotFound"})

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
                        body, cookie = bootstrap(handle.sessions, payload, shared=shared)
                        if body is None:
                            self.respond(401, {"error": "AuthenticationError"})
                        else:
                            self.respond(200, body, cookie=cookie)
                        return
                    valid, binding = handle.sessions.lookup(
                        self.headers.get("Cookie"), token=self.headers.get(SESSION_HEADER),
                    )
                    if not valid:
                        self.respond(
                            401,
                            {"error": "AuthenticationError", "message": "请从宿主重新打开记忆管理"},
                        )
                        return
                    self.respond(*dispatch(verb, payload, binding, self.client_address[0]))
                except Exception as error:
                    self.respond(*request_error(error))

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

    def launch_url(self, write_scope=None, *, page_bound=False):
        if self.closed:
            return None
        token = self.sessions.issue(deepcopy(write_scope), page_bound=page_bound)
        return self.address + "#access=" + token

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
            self.sessions.close()


class PortalHandle(_PortalServer):
    """Owns only web resources. Closing never closes the borrowed memory Runtime."""

    def __init__(
        self, runtime, security_provider, *, identity=None, port=0,
        configuration_conflict=False,
    ):
        self.runtime = runtime
        self.owner = deepcopy(security_provider().auth.actor)
        self._providers_lock = Lock()
        self._providers = {
            identity or new_provider_key(): {
                "provider": security_provider, "owner": self.owner,
                "configuration_conflict": configuration_conflict,
            },
        }
        super().__init__(self.dispatch, port=port)

    def register_provider(self, identity, security_provider, *, configuration_conflict=False):
        """Register one credential source; identical sources share an entry."""
        actor = deepcopy(security_provider().auth.actor)
        with self._providers_lock:
            if self.closed:
                raise RuntimeError("portal is closed")
            entry = self._providers.get(identity)
            if entry is not None:
                if (
                    entry["provider"] is not security_provider
                    or entry["configuration_conflict"] != configuration_conflict
                ):
                    raise PermissionDeniedError(message="Portal provider key is already registered")
                return
            if actor != self.owner:
                raise PermissionDeniedError(message="Runtime is already bound to another identity")
            self._providers[identity] = {
                "provider": security_provider, "owner": actor,
                "configuration_conflict": configuration_conflict,
            }

    def unregister_provider(self, identity):
        with self._providers_lock:
            self._providers.pop(identity, None)

    def provider_entry(self, identity):
        """Resolve only the credential source bound when the grant was issued."""
        with self._providers_lock:
            if identity is None:
                return None
            return self._providers.get(identity)

    def launch_url(self, write_scope=None, *, identity=None, experience_space=None):
        if experience_space is not None:
            experience_space = portal_experience_space(experience_space)
        with self._providers_lock:
            if self.closed:
                return None
            if identity is None:
                if len(self._providers) != 1:
                    raise ValidationError("Portal provider identity is required")
                identity = next(iter(self._providers))
            if identity not in self._providers:
                raise PermissionDeniedError(message="Portal provider is not registered")
            return super().launch_url({
                "write_scope": write_scope, "identity": identity,
                "experience_space": experience_space,
            }, page_bound=experience_space is not None)

    def dispatch(self, verb, payload, binding, peer):
        identity = binding.get("identity") if isinstance(binding, dict) else None
        entry = self.provider_entry(identity)
        if entry is None:
            return 401, {"error": "AuthenticationError", "message": "请从宿主重新打开记忆管理"}
        auth = entry["provider"]().auth
        if auth.actor != entry["owner"]:
            raise PermissionDeniedError(message="personal instance identity changed")
        if verb in CONFIG_VERBS and entry["configuration_conflict"]:
            return 409, {
                "error": "ConflictError",
                "message": "当前入口与运行中的记忆实例使用了不同的配置库",
            }
        return 200, runtime_request(
            self.runtime, auth, verb, payload, binding.get("write_scope"), peer=peer,
            experience_space=binding.get("experience_space"),
        )

    def close(self):
        super().close()
        with self._providers_lock:
            self._providers.clear()
        with _registry_lock:
            if _handles.get(id(self.runtime), lambda: None)() is self:
                del _handles[id(self.runtime)]


_registry_lock = RLock()
_handles = {}


def start_portal(
    runtime, *, security_provider, identity=None, port=0,
    configuration_conflict=False,
):
    """Attach to an existing instance; the host must call once and own the handle."""
    identity = identity or new_provider_key()
    with _registry_lock:
        existing = _handles.get(id(runtime), lambda: None)()
        if existing is not None and not existing.closed:
            if port and port != existing.server.server_port:
                raise ValidationError("Runtime already has a Portal on another port")
            existing.register_provider(
                identity, security_provider, configuration_conflict=configuration_conflict,
            )
            return existing
        handle = PortalHandle(
            runtime, security_provider, identity=identity, port=port,
            configuration_conflict=configuration_conflict,
        )
        _handles[id(runtime)] = ref(handle)
        return handle
