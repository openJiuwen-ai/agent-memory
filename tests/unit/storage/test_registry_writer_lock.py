"""Real operating-system Registry writer exclusion, including Windows byte locking."""

import subprocess
import sys

import pytest

from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit


def test_registry_writer_conflicts_across_processes_and_releases(tmp_path):
    path = tmp_path / "nested" / "registry.db"
    store = SQLiteKVStore(str(path))
    code = """
import sys
from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
store = SQLiteKVStore(sys.argv[1])
try:
    store.claim_single_writer('continuity')
except ConflictError:
    print('conflict')
else:
    print('acquired')
finally:
    store.close()
"""

    def attempt():
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(path)],
                                capture_output=True, text=True, timeout=15, check=True)
        return result.stdout.strip()

    try:
        store.claim_single_writer("continuity")
        assert attempt() == "conflict"
        store.release_single_writer("continuity")
        assert attempt() == "acquired"
        store.claim_single_writer("continuity")
    finally:
        store.close()
    assert attempt() == "acquired"
