from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatch

import pytest

from common.security.cryptography import CryptographyProvider
from common.type_def import (
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
    memory_key,
    messages_key,
)
from common.type_def.memory_codec import dumps, loads
from storage.kv_impl.encrypted_kv_store import EncryptedKVStore
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from storage.kv_impl.redis_kv import RedisKVStore
from storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit


class _FakeRedisClient:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def set(self, key, value, *, nx=False, xx=False, px=None):
        _ = px
        if nx and key in self.data:
            return False
        if xx and key not in self.data:
            return False
        self.data[key] = value
        return True

    def get(self, key):
        return self.data.get(key)

    def exists(self, key):
        return int(key in self.data)

    def delete(self, key):
        self.data.pop(key, None)

    def scan_iter(self, match):
        return [key for key in self.data if fnmatch(key, match)]

    def mget(self, keys):
        return [self.data.get(key) for key in keys]

    @staticmethod
    def ping():
        return True


class _FakeRedisKVStore(RedisKVStore):
    def __init__(self) -> None:
        super().__init__()
        self.fake_client = _FakeRedisClient()

    @property
    def client(self):
        return self.fake_client


class _ReverseSecurity(CryptographyProvider):
    def encrypt(self, plaintext, *, context=None, aad=b""):
        _ = context, aad
        return plaintext[::-1]

    def decrypt(self, ciphertext, *, context=None, aad=b""):
        _ = context, aad
        return ciphertext[::-1]


@pytest.fixture(params=("memory", "sqlite", "redis", "encrypted"))
def kv_store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryKVStore()
        return
    if request.param == "sqlite":
        store = SQLiteKVStore(str(tmp_path / "memory.sqlite3"))
        try:
            yield store
        finally:
            store.close()
        return
    if request.param == "redis":
        yield _FakeRedisKVStore()
        return
    yield EncryptedKVStore(InMemoryKVStore(), _ReverseSecurity())


def _unit(
    unit_id: str,
    scope: Scope,
    *,
    day: int,
    memory_type: str,
    project: str,
    priority: int,
) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=scope,
        segments=[Segment(content=unit_id)],
        temporal=Temporal(t_ingest=datetime(2026, 7, day, tzinfo=timezone.utc)),
        metadata={
            "memory_type": memory_type,
            "project": project,
            "priority": priority,
        },
    )


def _seed(store, scope: Scope) -> dict[str, MemoryUnit]:
    units = {
        "u1": _unit(
            "u1",
            scope,
            day=2,
            memory_type="coding",
            project="alpha",
            priority=1,
        ),
        "u2": _unit(
            "u2",
            scope,
            day=2,
            memory_type="coding",
            project="alpha",
            priority=2,
        ),
        "u3": _unit(
            "u3",
            scope,
            day=3,
            memory_type="episodic",
            project="alpha",
            priority=3,
        ),
    }
    for unit in units.values():
        store.insert(scope, memory_key(unit.id), dumps(unit))
    store.insert(scope, messages_key("raw"), dumps(units["u3"]))
    other = Scope(org=scope.org, space=scope.space, user="other")
    other_unit = _unit(
        "u2",
        other,
        day=4,
        memory_type="coding",
        project="alpha",
        priority=4,
    )
    store.insert(other, memory_key("u2"), dumps(other_unit))
    return units


def test_kv_list_filters_counts_sorts_and_paginates(kv_store) -> None:
    scope = Scope(org="acme", space="coding", user="alice")
    _seed(kv_store, scope)
    filters = FilterGroup(
        FilterLogic.AND,
        [
            FilterClause("metadata.project", FilterOp.EQ, "alpha"),
            FilterClause("metadata.priority", FilterOp.GTE, 1),
        ],
    )
    extensions = {"vendor_mode": "strict"}

    result = kv_store.list(
        scope,
        offset=1,
        limit=1,
        memory_types=["coding"],
        filters=filters,
        extensions=extensions,
    )

    assert result.count == 2
    assert [loads(raw).id for _, raw in result.entries] == ["u1"]
    assert extensions == {"vendor_mode": "strict"}


def test_kv_list_excludes_messages_and_keeps_count_beyond_last_page(kv_store) -> None:
    scope = Scope(org="acme", space="coding", user="alice")
    _seed(kv_store, scope)

    result = kv_store.list(scope, offset=10, limit=1)
    empty = kv_store.list(
        scope,
        filters=FilterClause("metadata.project", FilterOp.EQ, "missing"),
    )

    assert result.entries == []
    assert result.count == 3
    assert empty.entries == []
    assert empty.count == 0
