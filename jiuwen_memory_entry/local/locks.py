# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""File locks and private directories shared by the local service and the Portal.

The platform-specific primitives live next to the Portal IPC transport
(``portal/_posix_ipc`` / ``portal/_windows_ipc``); this module owns the
cross-platform contract: a non-blocking lock with a bounded wait, a context
manager, and the private-directory check.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    from jiuwen_memory_entry.portal import _windows_ipc as _ipc
else:
    from jiuwen_memory_entry.portal import _posix_ipc as _ipc


def private_directory(path: Path) -> Path:
    """Create (or verify) a directory readable only by the current user."""
    return _ipc.private_directory(path)


def acquire_file_lock(path: Path, *, timeout: float, conflict_message: str) -> int:
    """Take an exclusive lock on ``path``, waiting at most ``timeout`` seconds."""
    descriptor = _ipc.open_lock(path)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _ipc.lock(descriptor)
                return descriptor
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    # Lazy import keeps this helper importable from short-lived host
                    # hooks without pulling the kernel; only the conflict path pays.
                    from jiuwen_memory.api import ConflictError

                    raise ConflictError(message=conflict_message) from None
                time.sleep(0.025)
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def file_lock(
    path: Path, *, timeout: float = 0, conflict_message: str = "lock is already held"
) -> Iterator[int]:
    """Exclusive lock held for the duration of the ``with`` block."""
    descriptor = acquire_file_lock(
        path, timeout=timeout, conflict_message=conflict_message
    )
    try:
        yield descriptor
    finally:
        release_file_lock(descriptor)


def release_file_lock(descriptor: int) -> None:
    """Release a descriptor returned by :func:`acquire_file_lock`."""
    _ipc.close_lock(descriptor)
