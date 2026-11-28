"""Cross-platform file locks and private directories (F17 D2 / D5)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys

import pytest

from jiuwen_memory.common.errors import ConflictError, PermissionDeniedError
from jiuwen_memory_entry.local.locks import (
    acquire_file_lock,
    file_lock,
    private_directory,
    release_file_lock,
)

pytestmark = pytest.mark.unit


def test_lock_is_exclusive_and_reusable(tmp_path) -> None:
    path = tmp_path / "start.lock"
    descriptor = acquire_file_lock(path, timeout=0.5, conflict_message="busy")
    try:
        assert os.fstat(descriptor).st_size >= 0
    finally:
        release_file_lock(descriptor)
    with file_lock(path, timeout=0.5) as held:
        assert isinstance(held, int)
    # Released after the context manager: a second acquisition succeeds again.
    with file_lock(path, timeout=0.5):
        pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock holder used by the fixture")
def test_lock_conflict_with_holding_process(tmp_path) -> None:
    path = tmp_path / "start.lock"
    script = (
        "import fcntl, os, sys, time\n"
        f"fd = os.open({str(path)!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('ready', flush=True)\n"
        "sys.stdin.read()\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(ConflictError) as caught:
            acquire_file_lock(path, timeout=0.2, conflict_message="service start is busy")
        assert "service start is busy" in str(caught.value)
    finally:
        holder.kill()
        holder.wait(timeout=5)
    descriptor = acquire_file_lock(path, timeout=2.0, conflict_message="busy")
    release_file_lock(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are the assertion")
def test_private_directory_rejects_other_users_bits(tmp_path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    with pytest.raises(PermissionDeniedError):
        private_directory(loose)
    private = private_directory(tmp_path / "private")
    mode = stat.S_IMODE(private.lstat().st_mode)
    assert mode & 0o077 == 0, f"private directory is not private: {oct(mode)}"
