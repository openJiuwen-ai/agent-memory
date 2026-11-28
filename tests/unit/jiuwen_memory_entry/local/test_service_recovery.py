"""Per-runtime recovery thread lifecycle."""

from __future__ import annotations

import os
import threading

import pytest

from jiuwen_memory_entry.local.home import MemoryHome
from jiuwen_memory_entry.local.service import LocalService

pytestmark = pytest.mark.unit


def test_stop_recovery_sets_the_runtime_event_and_joins(tmp_path):
    service = LocalService(MemoryHome(tmp_path / "home"))
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, daemon=True)
    worker.start()
    setattr(service, "_recovery_stop", stop)
    setattr(service, "_recovery_thread", worker)
    service.stop_recovery()
    assert stop.is_set()
    assert not worker.is_alive()
    assert vars(service)["_recovery_stop"] is None
    assert vars(service)["_recovery_thread"] is None


def test_startup_repairs_modes_of_an_upgraded_data_tree(tmp_path):
    """Older installs predate the 0700 layout; the service must fix them at startup."""
    from jiuwen_memory_entry.local.service import harden_private_paths

    home = MemoryHome(tmp_path / "home")
    for directory in (home.root, home.run_dir, home.installs_dir, home.data_dir):
        directory.mkdir(parents=True, exist_ok=True)
    secret = home.data_dir / "kv.db"
    secret.write_text("memory content", encoding="utf-8")
    os.chmod(home.data_dir, 0o755)
    os.chmod(secret, 0o644)

    harden_private_paths(home)

    assert home.data_dir.stat().st_mode & 0o777 == 0o700
    assert home.root.stat().st_mode & 0o777 == 0o700
    assert secret.stat().st_mode & 0o777 == 0o600
