# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Same-account named pipes and file locks for the Windows shared Portal."""

import ctypes
import errno
import hashlib
import math
import msvcrt
import os
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from ctypes import wintypes
from functools import cache
from pathlib import Path
from struct import Struct
from threading import Event

import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32security
import winerror

from jiuwen_memory.api import PermissionDeniedError

_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_SECURITY_IDENTIFICATION = 0x00110000  # SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION
_CHUNK_SIZE = 64 * 1024
_LENGTH = Struct("!I")
_DISCONNECTED = (winerror.ERROR_BROKEN_PIPE, winerror.ERROR_NO_DATA)
_CANCEL_IO = ctypes.WinDLL("kernel32", use_last_error=True).CancelIoEx
_CANCEL_IO.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
_CANCEL_IO.restype = wintypes.BOOL


@cache
def _owner_sid():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        return user
    finally:
        token.Close()


def _private_attributes(*, directory=False):
    owner = win32security.ConvertSidToStringSid(_owner_sid())
    inheritance = "OICI" if directory else ""
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.bInheritHandle = False
    sddl = f"O:{owner}D:P(A;{inheritance};GA;;;{owner})"
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1)
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def state_root():
    folder = os.environ.get("LOCALAPPDATA", "")
    if not folder or not Path(folder).is_absolute():
        raise OSError("Windows LocalAppData directory is unavailable")
    return Path(folder).resolve(strict=True) / "jiuwen-memory-portal"


def private_directory(path):
    try:
        win32file.CreateDirectory(str(path), _private_attributes(directory=True))
    except pywintypes.error as exc:
        if exc.winerror != winerror.ERROR_ALREADY_EXISTS:
            raise OSError(exc.winerror, exc.strerror) from exc
    info = path.lstat()
    reparse_point = info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if not stat.S_ISDIR(info.st_mode) or reparse_point:
        raise PermissionDeniedError(message="shared Portal directory must be private")
    descriptor = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    acl = descriptor.GetSecurityDescriptorDacl()
    owner = _owner_sid()
    if descriptor.GetSecurityDescriptorOwner() != owner or acl is None or acl.GetAceCount() != 1:
        raise PermissionDeniedError(message="shared Portal directory must be private")
    (ace_type, _), _, trustee = acl.GetAce(0)
    control, _ = descriptor.GetSecurityDescriptorControl()
    if (
        trustee != owner or ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE
        or not control & win32security.SE_DACL_PROTECTED
    ):
        raise PermissionDeniedError(message="shared Portal directory must be private")
    return path


def open_lock(path):
    return os.open(path, os.O_CREAT | os.O_RDWR | os.O_BINARY, 0o600)


def lock(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise BlockingIOError(exc.errno, exc.strerror) from exc
        raise


def close_lock(descriptor):
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(descriptor)


def child_start_options(_start_lock):
    return [], {"creationflags": subprocess.DETACHED_PROCESS}


def _pipe_name(path):
    owner = win32security.ConvertSidToStringSid(_owner_sid())
    key = hashlib.sha256(f"{owner}\0{path}".encode()).hexdigest()
    return rf"\\.\pipe\jiuwen-memory-portal-{key}"


def _wait_for_pipe(name, milliseconds):
    try:
        win32pipe.WaitNamedPipe(name, milliseconds)
    except pywintypes.error as exc:
        if exc.winerror == winerror.ERROR_FILE_NOT_FOUND:
            raise FileNotFoundError(name) from exc
        if exc.winerror == winerror.ERROR_SEM_TIMEOUT:
            raise TimeoutError("shared Portal pipe is busy") from exc
        raise OSError(exc.winerror, exc.strerror) from exc


def listening(path, timeout):
    try:
        _wait_for_pipe(_pipe_name(path), max(1, math.ceil(timeout * 1000)))
    except FileNotFoundError:
        return False
    except TimeoutError:
        return True  # The pipe exists, but its listener is busy.
    return True


@contextmanager
def _connect(path, deadline):
    name = _pipe_name(path)
    while True:
        try:
            handle = win32file.CreateFile(
                name, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None,
                win32con.OPEN_EXISTING, win32con.FILE_FLAG_OVERLAPPED | _SECURITY_IDENTIFICATION,
                None,
            )
            break
        except pywintypes.error as exc:
            if exc.winerror == winerror.ERROR_FILE_NOT_FOUND:
                raise FileNotFoundError(name) from exc
            if exc.winerror != winerror.ERROR_PIPE_BUSY:
                raise OSError(exc.winerror, exc.strerror) from exc
            wait = _remaining_ms(deadline)
            if not wait:
                raise TimeoutError("shared Portal pipe is busy") from exc
            _wait_for_pipe(name, wait)
    try:
        descriptor = win32security.GetSecurityInfo(
            handle, win32security.SE_KERNEL_OBJECT, win32security.OWNER_SECURITY_INFORMATION,
        )
        if descriptor.GetSecurityDescriptorOwner() != _owner_sid():
            raise PermissionError("shared Portal pipe is owned by another Windows account")
        yield handle
    finally:
        handle.Close()


def _remaining_ms(deadline):
    return max(0, math.ceil((deadline - time.monotonic()) * 1000))


def _overlapped():
    io = pywintypes.OVERLAPPED()
    event = win32event.CreateEvent(None, True, False, None)
    io.hEvent = event
    return io, event


def _cancel(handle, io):
    # There is at most one pending operation on this handle. Drain it before
    # allowing the OVERLAPPED object or its read/write buffer to be released.
    _CANCEL_IO(int(handle), None)
    with suppress(pywintypes.error):
        win32file.GetOverlappedResult(handle, io, True)


def _complete(handle, io, event, status, deadline):
    if status == winerror.ERROR_IO_PENDING:
        try:
            result = win32event.WaitForSingleObject(event, _remaining_ms(deadline))
            if result == win32event.WAIT_TIMEOUT:
                raise TimeoutError("shared Portal pipe operation timed out")
        except BaseException:
            _cancel(handle, io)
            raise
    return win32file.GetOverlappedResult(handle, io, False)


def _read_exact(handle, count, deadline):
    data = bytearray()
    while len(data) < count:
        io, event = _overlapped()
        try:
            status, buffer = win32file.ReadFile(handle, min(_CHUNK_SIZE, count - len(data)), io)
            read = _complete(handle, io, event, status, deadline)
            if not read:
                raise ConnectionError("shared Portal pipe closed")
            data.extend(buffer[:read])
        finally:
            event.Close()
    return bytes(data)


def _receive(handle, limit, deadline):
    size, = _LENGTH.unpack(_read_exact(handle, _LENGTH.size, deadline))
    if not 0 < size <= limit:
        raise ValueError("IPC message size is invalid")
    return _read_exact(handle, size, deadline)


def _send(handle, data, limit, deadline):
    if not 0 < len(data) <= limit:
        raise ValueError("IPC message size is invalid")
    packet = _LENGTH.pack(len(data)) + data
    io, event = _overlapped()
    try:
        status, _ = win32file.WriteFile(handle, packet, io)
        if _complete(handle, io, event, status, deadline) != len(packet):
            raise ConnectionError("shared Portal pipe write incomplete")
    finally:
        event.Close()


def exchange(path, payload, timeout, limit):
    deadline = time.monotonic() + timeout
    try:
        with _connect(path, deadline) as handle:
            _send(handle, payload, limit, deadline)
            return _receive(handle, limit, deadline)
    except pywintypes.error as exc:
        # Once connected, a pipe error does not prove the request was undelivered.
        raise OSError(exc.winerror, exc.strerror) from exc


class _PipeAccept:
    def __init__(self, name, *, first=False):
        flags = win32pipe.PIPE_ACCESS_DUPLEX | win32con.FILE_FLAG_OVERLAPPED
        if first:
            flags |= _FIRST_PIPE_INSTANCE
        self.handle = win32pipe.CreateNamedPipe(
            name, flags, _PIPE_REJECT_REMOTE_CLIENTS, win32pipe.PIPE_UNLIMITED_INSTANCES,
            _CHUNK_SIZE, _CHUNK_SIZE, 0, _private_attributes(),
        )
        try:
            self.io, self.event = _overlapped()
            try:
                self.pending = self._connect()
            except BaseException:
                self.event.Close()
                raise
        except BaseException:
            self.handle.Close()
            raise

    def _connect(self):
        try:
            return win32pipe.ConnectNamedPipe(self.handle, self.io) == winerror.ERROR_IO_PENDING
        except pywintypes.error as exc:
            # A short-lived peer may have disconnected before ConnectNamedPipe.
            if exc.winerror not in _DISCONNECTED:
                raise
            return False

    def ready(self, timeout):
        if not self.pending:
            return True
        result = win32event.WaitForSingleObject(self.event, math.ceil(timeout * 1000))
        if result == win32event.WAIT_TIMEOUT:
            return False
        self.pending = False
        try:
            win32file.GetOverlappedResult(self.handle, self.io, False)
        except pywintypes.error as exc:
            if exc.winerror not in _DISCONNECTED:
                raise
        return True

    def take(self):
        self.event.Close()
        return self.handle

    def close(self):
        try:
            if self.pending:
                _cancel(self.handle, self.io)
        finally:
            self.event.Close()
            self.handle.Close()


class _PipeServer:
    def __init__(self, path, *, threaded, interval, request_timeout, limit):
        self._name = _pipe_name(path)
        self._interval = interval
        self._request_timeout = request_timeout
        self._limit = limit
        self._stop = Event()
        self._workers = ThreadPoolExecutor(max_workers=16) if threaded else None
        self.dispatch = None
        self._listener = _PipeAccept(self._name, first=True)

    def handle_request(self):
        listener = self._listener
        if not listener.ready(self._interval):
            return
        # Keep a pipe instance open to reserve the name even while handling a call.
        self._listener = _PipeAccept(self._name)
        handle = listener.take()
        if self._workers is None:
            self._handle(handle)
        else:
            try:
                self._workers.submit(self._handle, handle)
            except BaseException:
                handle.Close()
                raise

    def _handle(self, handle):
        try:
            raw = _receive(handle, self._limit, time.monotonic() + self._request_timeout)
            response = self.dispatch(raw)
            deadline = time.monotonic() + self._request_timeout
            _send(handle, response, self._limit, deadline)
            # The client closes after reading. Wait for that with a deadline
            # so the server does not close with unread response data.
            _read_exact(handle, 1, deadline)
        except (pywintypes.error, OSError, TypeError, ValueError, KeyError):
            return
        finally:
            handle.Close()

    def serve_forever(self):
        while not self._stop.is_set():
            self.handle_request()

    def shutdown(self):
        self._stop.set()

    def server_close(self):
        try:
            self._listener.close()
        finally:
            if self._workers is not None:
                self._workers.shutdown(wait=True)


def server(path, *, threaded, interval, request_timeout, limit):
    try:
        return _PipeServer(
            path, threaded=threaded, interval=interval,
            request_timeout=request_timeout, limit=limit,
        )
    except pywintypes.error as exc:
        raise OSError(exc.winerror, exc.strerror) from exc


def mark_source(path):
    path.touch(exist_ok=False)


def source_endpoints(directory):
    return (path for path in directory.iterdir() if path.is_file() and not path.is_symlink())
