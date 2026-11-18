# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A browser listener shared by SDK processes using the same local document store."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4

from jiuwen_memory.api import (
    AuthenticationError,
    BackendError,
    ConflictError,
    Scope,
    ValidationError,
)
from jiuwen_memory_entry.core.api_contract import CONFIG_VERBS
from jiuwen_memory_entry.portal.hosting import _PortalServer, request_error, runtime_request
from jiuwen_memory_entry.portal.multi_agent import VIEW_VALUE
from jiuwen_memory_entry.portal.requests import MULTI_AGENT_VERBS

if os.name == "nt":
    from jiuwen_memory_entry.portal import _windows_ipc as _ipc
else:
    from jiuwen_memory_entry.portal import _posix_ipc as _ipc

_START_TIMEOUT = 10
_PROBE_INTERVAL = 0.5
_PROBE_TIMEOUT = 0.25
_CONTROL_TIMEOUT = 5
_MAX_MESSAGE = 64 * 1024 * 1024
_REGISTRATION = re.compile(r"[0-9a-f]{32}")
_OFFLINE = (401, {"error": "AuthenticationError", "message": "请从宿主重新打开记忆管理"})
_AUTH_REJECTED = {"error": "AuthenticationError", "dispatched": False}
_IDENTITY_CHANGED = _AUTH_REJECTED | {"identity_changed": True}
_INSTANCE_UNAVAILABLE = (404, {"error": "NotFoundError", "message": "配置实例不可用，请刷新配置"})
_MODULE = "jiuwen_memory_entry.portal.shared"
_START_ERROR = re.compile(
    r"^((?:[A-Za-z_][A-Za-z_0-9.]{0,79})?(?:Error|Exception)|pywintypes\.error): (.*)$",
)
_OS_ERROR = re.compile(r"^\[(Errno|WinError) ([0-9]{1,5})\]")
_PIPE_ERROR = re.compile(r"^\(([0-9]{1,5}),")
_MISSING_MODULE = re.compile(r"^No module named '([A-Za-z_][A-Za-z_0-9.]{0,79})'$")
logger = logging.getLogger(__name__)


class _StartupStderr:
    """Drain stderr for the lifetime of the child, retaining only its startup tail."""

    def __init__(self):
        self._tail: bytes | None = b""
        self._lock = Lock()

    def drain(self, stream):
        with stream:
            while True:
                chunk = stream.read1(1024)
                if not chunk:
                    break
                with self._lock:
                    if self._tail is not None:
                        self._tail = (self._tail + chunk)[-4096:]

    def discard(self):
        with self._lock:
            self._tail = None

    def summary(self):
        with self._lock:
            tail = self._tail or b""
        for line in reversed(tail.decode(errors="replace").splitlines()):
            match = _START_ERROR.fullmatch(line)
            if match is not None:
                return self._safe_exception(*match.groups())
        return "no safe error details"

    @staticmethod
    def _safe_exception(kind, detail):
        # Tracebacks and free-form messages can contain local paths or credentials.
        os_error = _OS_ERROR.match(detail)
        if os_error is not None:
            label, code = os_error.groups()
            description = f" {os.strerror(int(code))}" if label == "Errno" else ""
            return f"{kind} [{label} {code}]{description}"
        if kind == "pywintypes.error":
            pipe_error = _PIPE_ERROR.match(detail)
            if pipe_error is not None:
                return f"{kind} [WinError {pipe_error[1]}]"
        if kind == "ModuleNotFoundError":
            module = _MISSING_MODULE.fullmatch(detail)
            if module is not None:
                return f"{kind}: No module named '{module[1]}'"
        return kind


class _InstanceDisconnected(ConnectionError):
    """The IPC call could not connect, so no request was delivered."""


@dataclass(frozen=True)
class _Registration:
    endpoint: str
    instance_id: str
    identity: str | None
    runtime_key: str | None


@dataclass(frozen=True)
class _BrowserAccess:
    identity: str
    preferred_instance: str
    registration: str
    write_scope: dict | None
    provider_bound: bool


def _identity(auth, *, bind_credential=True):
    # Match the trusted identity and credential, independent of authentication timestamps.
    return hashlib.sha256(_encode((
        asdict(auth.actor), auth.role, auth.credential_type,
        auth.credential_id if bind_credential else "", auth.auth_method,
        auth.credential_issuer, auth.credential_status_required, auth.delegation_id,
    ))).hexdigest()


def _acquire_file_lock(path: Path, *, timeout: float, conflict_message: str) -> int:
    descriptor = _ipc.open_lock(path)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _ipc.lock(descriptor)
                return descriptor
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConflictError(message=conflict_message) from None
                time.sleep(0.025)
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _lock_file(path: Path, *, timeout: float = 0) -> Iterator[int]:
    descriptor = _acquire_file_lock(
        path, timeout=timeout, conflict_message="shared Portal lock is already held",
    )
    try:
        yield descriptor
    finally:
        _ipc.close_lock(descriptor)


class SharedPortalLocation:
    """Discover and start one Portal per local account and document store."""

    def __init__(self, paths: tuple[str, str]):
        digest = hashlib.sha256(os.fsencode("\0".join(paths))).hexdigest()[:32]
        self._base = _ipc.private_directory(_ipc.state_root())
        self._root = paths[0]
        self.directory = _ipc.private_directory(self._base / digest)
        self.endpoint = self.directory / "portal.sock"
        self.start_lock = self.directory / "start.lock"

    def claim_instance(self, instance_id: str, resources):
        # Each document root has one overlay database, even with an explicit index path.
        digest = hashlib.sha256(os.fsencode(f"{self._root}\0{instance_id}")).hexdigest()[:32]
        descriptor = _acquire_file_lock(
            self._base / f"instance-{digest}.lock", timeout=0,
            conflict_message=f"Portal instance_id {instance_id!r} is already in use",
        )
        # Only explicit Runtime cleanup releases this descriptor. A failed shutdown retains it.
        resources.callback(_ipc.close_lock, descriptor)

    def register(self, registration, info):
        with _lock_file(self.start_lock, timeout=_START_TIMEOUT) as start_lock:
            self._ensure_started(info, start_lock)
            status, result = _call(self.endpoint, {
                "action": "register_source", "registration": registration, **info,
            })
            if status == 409:
                raise ConflictError(message=result["message"])
            if status != 200:
                raise BackendError("could not register the SDK instance with the shared Portal")
            return result

    def _ensure_started(self, info, start_lock):
        process = None
        reader = None
        stderr = _StartupStderr()
        try:
            deadline = time.monotonic() + _START_TIMEOUT
            while time.monotonic() < deadline:
                if _listening(self.endpoint):
                    if os.name != "nt" or _ready(self.endpoint):
                        stderr.discard()
                        return
                elif process is None:
                    process = self._start(info["port"], start_lock)
                    reader = Thread(target=stderr.drain, args=(process.stderr,), daemon=True)
                    reader.start()
                if process is not None and process.poll() is not None:
                    break
                time.sleep(0.025)
            exit_code = process.poll() if process is not None else None
            if exit_code is None or reader is None:
                logger.warning(
                    "shared Portal startup timed out after %s s; stderr: %s",
                    _START_TIMEOUT, stderr.summary(),
                )
            else:
                reader.join(timeout=_PROBE_TIMEOUT)
                logger.warning(
                    "shared Portal exited before becoming ready (exit code %s); stderr: %s",
                    exit_code, stderr.summary(),
                )
            raise BackendError("shared Portal process could not start")
        except BaseException:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
            raise
        finally:
            if process is not None and process.poll() is None:
                Thread(target=process.wait, daemon=True).start()

    def _start(self, port, start_lock):
        self.endpoint.unlink(missing_ok=True)
        # The host may have added the SDK to sys.path after Python started.
        env = os.environ | {
            "PYTHONPATH": os.pathsep.join(path for path in sys.path if isinstance(path, str)),
        }
        lock_args, startup = _ipc.child_start_options(start_lock)
        return subprocess.Popen(
            [
                sys.executable, "-m", _MODULE, str(self.directory), str(port),
                *lock_args,
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=env, **startup,
        )


def _listening(path: Path) -> bool:
    return _ipc.listening(path, _PROBE_TIMEOUT)


def _ready(path):
    try:
        return _call(path, {"action": "ready"}, timeout=_PROBE_TIMEOUT) == [200, None]
    except (ConnectionError, TimeoutError):
        return False


def _encode(value):
    return json.dumps(value, ensure_ascii=False).encode() + b"\n"


def _call(path: Path, request, *, timeout=_CONTROL_TIMEOUT):
    try:
        response = _ipc.exchange(path, _encode(request), timeout, _MAX_MESSAGE)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise _InstanceDisconnected(str(path)) from exc
    if not response:
        raise ConnectionError("local Portal connection closed")
    return json.loads(response)


def _server(path, *, threaded):
    return _ipc.server(
        path, threaded=threaded, interval=_PROBE_INTERVAL,
        request_timeout=_CONTROL_TIMEOUT, limit=_MAX_MESSAGE,
    )


class SharedPortalHandle:
    def __init__(
        self, runtime, provider, location, instance_id, *,
        port, runtime_key=None, multi_agent=None,
    ):
        self._location = location
        self._instance_id = instance_id
        self._provider = provider
        self._registration = uuid4().hex
        self._multi_agent = multi_agent
        self._endpoint = location.directory / self._registration
        self._lock = Lock()
        self.closed = False
        self.address = None
        auth = provider().auth
        self._owner = _identity(auth, bind_credential=False)
        self._info = {
            "instance_id": instance_id, "identity": _identity(auth), "port": port,
            "runtime_key": runtime_key,
        }
        self._server = _server(self._endpoint, threaded=True)

        def dispatch(message):
            try:
                if message.get("action") == "describe_source":
                    if self.address is None or self.closed:
                        return 404, None
                    return 200, self._info
                expected = self._info["identity"]
                if message["identity"] != expected:
                    return 401, _AUTH_REJECTED
                try:
                    auth = self._provider().auth
                except AuthenticationError:
                    return 401, _AUTH_REJECTED
                if _identity(auth) != expected:
                    return 401, _IDENTITY_CHANGED
                if auth.is_expired():
                    return 401, _AUTH_REJECTED
                if message.get("action") == "check_identity":
                    return 200, None
                scope = message["write_scope"]
                return 200, runtime_request(
                    runtime, auth, message["verb"], message["payload"],
                    Scope(**scope) if scope else None,
                    multi_agent=(
                        self._multi_agent if message.get("view") == VIEW_VALUE else None
                    ),
                )
            except Exception as error:
                return request_error(error)

        def dispatch_json(raw):
            return _encode(dispatch(json.loads(raw)))

        self._server.dispatch = dispatch_json
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        try:
            _ipc.mark_source(self._endpoint)
            self._thread.start()
        except BaseException:
            self._server.server_close()
            self._endpoint.unlink(missing_ok=True)
            raise
        try:
            self._register()
        except BaseException:
            self.close()
            raise

    def _register(self, *, identity=None):
        info = self._info if identity is None else {**self._info, "identity": identity}
        self.address = self._location.register(self._registration, info)
        self._info = info

    def _control(self, action, *, timeout=_CONTROL_TIMEOUT, **fields):
        return _call(self._location.endpoint, {
            "action": action, "instance_id": self._instance_id, "registration": self._registration,
            **fields,
        }, timeout=timeout)

    @property
    def active_address(self):
        if self.closed:
            return None
        try:
            status, address = self._control("status", timeout=_PROBE_TIMEOUT)
        except OSError:
            return None
        return address if status == 200 else None

    def launch_url(self, write_scope=None):
        with self._lock:
            if self.closed:
                return None
            auth = self._provider().auth
            if auth.is_expired() or _identity(auth, bind_credential=False) != self._owner:
                raise AuthenticationError("personal instance identity changed")
            identity = _identity(auth)
            if identity != self._info["identity"]:
                self._register(identity=identity)
            payload = asdict(write_scope) if write_scope is not None else None
            try:
                status, link = self._control("launch", write_scope=payload)
            except ConnectionError:
                status = 404
            if status == 404:
                self._register()
                status, link = self._control("launch", write_scope=payload)
            if status != 200:
                raise BackendError("could not open the shared Portal")
            self.address = link.partition("#")[0].partition("?")[0]
            return link

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            try:
                self._control("unregister")
            except OSError:
                pass
            finally:
                self._server.shutdown()
                self._thread.join()
                self._server.server_close()
                self._endpoint.unlink(missing_ok=True)


class _Registry:
    def __init__(self, directory, port, live):
        self.directory = directory
        self.registrations: dict[str, _Registration] = {}
        self.lock = Lock()
        self.portal = _PortalServer(self.dispatch, port=port, shared=True)
        for endpoint, info in live:
            self.register(info, info["instance_id"], endpoint)

    def dispatch(self, verb, payload, access: _BrowserAccess, _peer, view=None):
        if view == VIEW_VALUE and verb not in MULTI_AGENT_VERBS | {"portal_context"}:
            raise ValidationError("multi-agent Portal is read-only")
        with self.lock:
            opening_instance = self.registrations.get(access.registration)
            candidates = [
                source for source in self.registrations.values()
                if source is not opening_instance and source.identity == access.identity
            ]
        if opening_instance is not None:
            if opening_instance.identity != access.identity:
                return _OFFLINE
            candidates.insert(0, opening_instance)
        elif access.provider_bound:
            return _OFFLINE
        else:
            candidates.sort(key=lambda source: source.instance_id != access.preferred_instance)
        if not candidates:
            return _OFFLINE
        if verb in CONFIG_VERBS:
            eligible = {}
            for source in candidates:
                eligible.setdefault(source.instance_id, []).append(source)
            return self.configuration(verb, payload, access, eligible, opening_instance)
        if access.provider_bound:
            # An SDK continuity session must never switch to another Provider.
            candidates = candidates[:1]
        for instance in candidates:
            try:
                status, result = self.forward(instance, verb, payload, access, view)
            except _InstanceDisconnected:
                continue
            except OSError:
                # A sent write may already have been executed. Do not replay it.
                return 503, {"error": "BackendError", "message": "记忆实例未响应"}
            if status == 401 and result in (_AUTH_REJECTED, _IDENTITY_CHANGED):
                # A stale backup rejected the request before calling the Runtime.
                if instance.endpoint == access.registration:
                    return _OFFLINE
                continue
            return status, result
        return _OFFLINE

    def configuration(self, verb, payload, access, eligible, opening_instance):
        instance_id = payload.get("instance_id")
        checked_endpoint = None
        if opening_instance is not None:
            try:
                status, result = self._call_source(
                    opening_instance, access.identity, {"action": "check_identity"},
                )
            except _InstanceDisconnected:
                if access.provider_bound:
                    return _OFFLINE
            except OSError:
                return 503, {"error": "BackendError", "message": "记忆实例未响应"}
            else:
                if status in (401, 403):
                    return _OFFLINE
                if status != 200:
                    return status, result
                checked_endpoint = opening_instance.endpoint

        if verb == "read_config" and not payload:
            instances = []
            for instance_id in sorted(eligible):
                for source in eligible[instance_id]:
                    if self._accepts_identity(source, access.identity, checked_endpoint):
                        instances.append(instance_id)
                        break
            if not instances:
                return _INSTANCE_UNAVAILABLE
            return 200, {
                "instances": instances,
                "default_instance_id": (
                    access.preferred_instance if access.preferred_instance in instances
                    else instances[0]
                ),
            }
        if not isinstance(instance_id, str) or not instance_id:
            raise ValidationError("shared Portal configuration requires instance_id")
        rejected = (403, {"error": "PermissionDeniedError", "message": "配置实例拒绝访问"})
        fields = {key: value for key, value in payload.items() if key != "instance_id"}
        failure = _INSTANCE_UNAVAILABLE
        for instance in eligible.get(instance_id, ()):
            try:
                status, result = self.forward(instance, verb, fields, access)
            except _InstanceDisconnected:
                continue
            except OSError:
                return 503, {
                    "error": "BackendError", "message": "配置实例未响应，请刷新后确认保存结果",
                }
            if status == 401 and result in (_AUTH_REJECTED, _IDENTITY_CHANGED):
                failure = rejected
                continue
            # A target rejecting credentials must not invalidate the browser session.
            return rejected if status == 401 else (status, result)
        return failure

    def forward(self, instance, verb, payload, access, view=None):
        return self._call_source(instance, access.identity, {
            "verb": verb, "payload": payload,
            "write_scope": access.write_scope,
            "view": view,
        }, timeout=300)

    def _accepts_identity(self, source, identity, checked_endpoint):
        if source.endpoint == checked_endpoint:
            return True
        try:
            status, _ = self._call_source(
                source, identity, {"action": "check_identity"}, timeout=_PROBE_TIMEOUT,
            )
        except OSError:
            return False
        return status == 200

    def _call_source(self, source, identity, request, *, timeout=_CONTROL_TIMEOUT):
        status, result = _call(
            self.directory / source.endpoint, request | {"identity": identity}, timeout=timeout,
        )
        if status == 401 and result == _IDENTITY_CHANGED:
            with self.lock:
                if self.registrations.get(source.endpoint) is source:
                    self.registrations[source.endpoint] = replace(source, identity=None)
                self._revoke(source.endpoint, identity)
        return status, result

    def _revoke(self, registration, identity):
        self.portal.sessions.revoke(
            lambda access: access.registration == registration and access.identity == identity,
        )

    def control(self, message):
        action = message["action"]
        if action == "ready":
            return 200, None
        instance_id, registration = message["instance_id"], message["registration"]
        if action == "register_source":
            return self.register(message, instance_id, registration)
        with self.lock:
            instance = self.registrations.get(registration)
            if instance is None or instance.instance_id != instance_id:
                return 404, None
            if action == "status":
                return 200, self.portal.address
            if action == "unregister":
                del self.registrations[registration]
                return 200, None
            if action != "launch" or instance.identity is None:
                return 404, None
            access = _BrowserAccess(
                instance.identity, instance_id, registration, message.get("write_scope"),
                provider_bound=instance.runtime_key is not None,
            )
            address, _, fragment = self.portal.launch_url(access).partition("#")
        # A new document navigation also replaces the old session when an iframe is reused.
        return 200, f"{address}?open={uuid4().hex}#{fragment}"

    def register(self, message, instance_id, registration):
        if not _REGISTRATION.fullmatch(registration):
            return 404, None
        if not _listening(self.directory / registration):
            return 404, None
        if message["port"] and message["port"] != self.portal.server.server_port:
            return 409, {"message": "shared Portal is already running on another port"}
        runtime_key = message.get("runtime_key")
        identity = message["identity"]
        with self.lock:
            for endpoint, existing in self.registrations.items():
                if existing.instance_id != instance_id or endpoint == registration:
                    continue
                if runtime_key is not None and runtime_key == existing.runtime_key:
                    continue
                if _listening(self.directory / endpoint):
                    return 409, {"message": "another runtime already uses this Portal instance ID"}
            previous = self.registrations.get(registration)
            if previous is not None and previous.identity not in (None, identity):
                self._revoke(registration, previous.identity)
            self.registrations[registration] = _Registration(
                registration, instance_id, identity, runtime_key,
            )
        return 200, self.portal.address

    def prune(self):
        # Control requests add and remove entries. Browser requests may invalidate credentials.
        with self.lock:
            registrations = tuple(self.registrations)
        for registration in registrations:
            endpoint = self.directory / registration
            if not _listening(endpoint):
                with self.lock:
                    del self.registrations[registration]
                endpoint.unlink(missing_ok=True)


def _live_registrations(directory):
    for path in _ipc.source_endpoints(directory):
        if not _REGISTRATION.fullmatch(path.name):
            continue
        try:
            status, info = _call(path, {"action": "describe_source"}, timeout=_PROBE_TIMEOUT)
        except _InstanceDisconnected:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        else:
            if status == 200:
                yield path.name, info


def _control_server(endpoint, resources):
    server = _server(endpoint, threaded=False)
    resources.callback(endpoint.unlink, missing_ok=True)
    resources.callback(server.server_close)
    return server


def _serve(directory, port, start_lock):
    endpoint = _ipc.private_directory(directory) / "portal.sock"
    with ExitStack() as resources:
        # Windows cannot inherit the launcher's lock. Reserve the pipe name
        # first so a second child cannot bind a Portal while this one starts.
        server = _control_server(endpoint, resources) if os.name == "nt" else None
        live = list(_live_registrations(directory))
        port = next((info["port"] for _, info in live if info["port"]), port)
        registry = _Registry(directory, port, live)
        resources.callback(registry.portal.close)
        if server is None:
            server = _control_server(endpoint, resources)
        if start_lock is not None:
            # POSIX retains the parent's lock until both listeners are bound.
            os.close(start_lock)

        def dispatch_control(raw):
            return _encode(registry.control(json.loads(raw)))

        server.dispatch = dispatch_control
        next_probe = time.monotonic() + _PROBE_INTERVAL
        while True:
            server.handle_request()
            now = time.monotonic()
            if now >= next_probe:
                registry.prune()
                next_probe = now + _PROBE_INTERVAL
            if registry.registrations:
                continue
            # Never wait here: a registering process can hold the lock while connecting.
            try:
                with _lock_file(directory / "start.lock"):
                    resources.close()
                    return
            except ConflictError:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("port", type=int)
    parser.add_argument("start_lock", type=int, nargs="?")
    args = parser.parse_args()
    _serve(args.directory, args.port, args.start_lock)
