# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unix-domain transport for a shared SDK Portal."""

import fcntl
import os
import socket
import stat
from pathlib import Path
from socketserver import StreamRequestHandler, ThreadingMixIn, UnixStreamServer

from jiuwen_memory.api import PermissionDeniedError

close_lock = os.close


def state_root():
    return Path(f"/tmp/jm-portal-{os.geteuid()}")


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionDeniedError(message="shared Portal directory must be private")
    return path


def open_lock(path):
    return os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)


def lock(descriptor):
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def child_start_options(start_lock):
    return [str(start_lock)], {"start_new_session": True, "pass_fds": (start_lock,)}


def listening(path, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        try:
            connection.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            return False
        except (BlockingIOError, TimeoutError):
            return True
        return True


def _read(stream, limit):
    raw = stream.readline(limit + 1)
    if len(raw) > limit:
        raise ValueError("IPC message is too large")
    return raw


def exchange(path, payload, timeout, limit):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        # Only connection failures before delivery may trigger failover.
        try:
            connection.sendall(payload)
            with connection.makefile("rb") as stream:
                return _read(stream, limit)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise ConnectionError("local Portal connection closed") from exc


class _Request(StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(self.server.request_timeout)
        try:
            raw = _read(self.rfile, self.server.message_limit)
            if raw:
                self.wfile.write(self.server.dispatch(raw))
        except (OSError, TypeError, ValueError, KeyError):
            return


class _RuntimeServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = False
    block_on_close = True


def server(path, *, threaded, interval, request_timeout, limit):
    server_type = _RuntimeServer if threaded else UnixStreamServer
    listener = server_type(str(path), _Request)
    listener.timeout = interval
    listener.request_timeout = request_timeout
    listener.message_limit = limit
    return listener


def mark_source(_path):
    """The bound Unix socket already identifies this registration."""


def source_endpoints(directory):
    return (path for path in directory.iterdir() if path.is_socket())
