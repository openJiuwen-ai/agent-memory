"""PR1-1: encrypted persistence survives reassembly while ordinary memory search works."""

from __future__ import annotations

import io

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.type_def import Context, Scope, memory_key
from jiuwen_memory.retrieval.types import DisclosureLevel
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
from jiuwen_memory.storage.store_manager import StoreManager
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.integration


def test_memory_api_search_and_persistent_assets_survive_reassembly(tmp_path):
    scope = Scope(org="acme", user="alice")
    security = internal_context(ScopedAuthenticator(scope))
    db_path = str(tmp_path / "memory.db")
    config = {
        "globals": {
            "storage_encryption_enabled": True,
            "vector_enabled": False,
            "graph_enabled": False,
        },
        "permission": {"default": "allow_all"},
        "key_provider": {"default": {"target": "local", "params": {"key_hex": "11" * 32}}},
        "cryptography": {"default": {"target": "local", "params": {"key_provider": "default"}}},
        "fs_store": {"default": {"target": "local", "params": {"root": str(tmp_path / "assets")}}},
    }
    raw = SQLiteKVStore(db_path=db_path)
    created = []
    with Factory.observe_builds(created.append):
        runtime = assemble_runtime(kv=raw, config=config)
    storage = next(obj for obj in created if isinstance(obj, StoreManager))
    try:
        units = runtime.api.add("project zephyr secret launch plan", scope, security=security)
        assert units
        unit_id = units[0].id
        assert raw.get(scope, memory_key(unit_id)).startswith(b"ENC1")
        result = runtime.api.search(
            "zephyr", Context(scope=scope), security=security, disclosure=DisclosureLevel.L2
        )
        assert result.items
        assert any("zephyr" in item.content for item in result.items)
        ref = storage.fs().insert(scope, "sample.bin", io.BytesIO(b"private asset"))
    finally:
        runtime.close()
        raw.close()
    reopened_raw = SQLiteKVStore(db_path=db_path)
    created = []
    with Factory.observe_builds(created.append):
        reopened = assemble_runtime(kv=reopened_raw, config=config)
    storage = next(obj for obj in created if isinstance(obj, StoreManager))
    try:
        assert storage.kv().get(scope, memory_key(unit_id))
        with storage.fs().get(scope, ref) as stream:
            assert stream.read() == b"private asset"
        assert b"private asset" not in b"".join(
            path.read_bytes() for path in (tmp_path / "assets").rglob("*.bin")
        )
    finally:
        reopened.close()
        reopened_raw.close()
