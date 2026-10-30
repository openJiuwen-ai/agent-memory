"""Native conditional KV writes, including independent SQLite connections."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage._privacy_kv import PrivacyKVStore
from jiuwen_memory.storage.kv_impl.encrypted_kv_store import EncryptedKVStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit
SCOPE = Scope(org="o", space="s", user="u", agent="a", session="x")


@pytest.fixture(params=["memory", "sqlite", "encrypted"])
def backend(request, tmp_path, privacy_crypto):
    if request.param == "sqlite":
        kv = SQLiteKVStore(str(tmp_path / "cas.sqlite"))
    else:
        kv = InMemoryKVStore()
    store = EncryptedKVStore(kv, privacy_crypto) if request.param == "encrypted" else kv
    yield store
    if hasattr(kv, "close"):
        kv.close()


def test_cas_distinguishes_absent_empty_mismatch_update_and_delete(backend):
    assert backend.supports_compare_and_swap()
    assert backend.compare_and_swap(SCOPE, "key", None, b"")
    assert not backend.compare_and_swap(SCOPE, "key", None, b"bad")
    assert not backend.compare_and_swap(SCOPE, "key", b"wrong", None)
    assert backend.get(SCOPE, "key") == b""
    assert backend.compare_and_swap(SCOPE, "key", b"", b"new")
    assert backend.compare_and_swap(SCOPE, "key", b"new", None)
    assert backend.compare_and_swap(SCOPE, "key", None, None)
    assert not backend.exists(SCOPE, "key")


@pytest.mark.parametrize("dimension", ["org", "space", "user", "agent", "session"])
def test_cas_isolates_all_scope_dimensions(backend, dimension):
    backend.insert(SCOPE, "key", b"original")
    other = replace(SCOPE, **{dimension: "other"})
    assert not backend.compare_and_swap(other, "key", b"original", b"changed")
    assert backend.compare_and_swap(other, "key", None, b"independent")
    assert backend.get(SCOPE, "key") == b"original"


def test_expired_values_compare_as_absent_and_success_clears_ttl(backend, monkeypatch):
    import time

    start = time.time()
    monkeypatch.setattr(time, "time", lambda: start)
    backend.insert(SCOPE, "key", b"old", ttl=10)
    monkeypatch.setattr(time, "time", lambda: start + 11)
    assert not backend.compare_and_swap(SCOPE, "key", b"old", b"wrong")
    assert backend.compare_and_swap(SCOPE, "key", None, b"new")
    monkeypatch.setattr(time, "time", lambda: start + 1000)
    assert backend.get(SCOPE, "key") == b"new"


@pytest.mark.parametrize("sqlite", [False, True])
def test_competing_writers_have_exactly_one_winner(tmp_path, sqlite):
    path = str(tmp_path / "shared.sqlite")
    first = SQLiteKVStore(path) if sqlite else InMemoryKVStore()
    second = SQLiteKVStore(path) if sqlite else first
    first.insert(SCOPE, "key", b"old")
    barrier = Barrier(2)

    def attempt(store, value):
        barrier.wait(timeout=10)
        return store.compare_and_swap(SCOPE, "key", b"old", value)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(attempt, first, b"one")
            b = pool.submit(attempt, second, b"two")
            assert sorted([a.result(timeout=15), b.result(timeout=15)]) == [False, True]
        assert first.get(SCOPE, "key") in (b"one", b"two")
    finally:
        if sqlite:
            first.close()
            second.close()


@pytest.mark.parametrize("prefix", ["/memory/", "/messages/"])
def test_privacy_wrapper_rejects_truth_bypass(prefix):
    store = PrivacyKVStore(InMemoryKVStore(), object())
    with pytest.raises(ValidationError):
        store.compare_and_swap(SCOPE, prefix + "one", None, b"unprotected")
    assert store.compare_and_swap(SCOPE, "/system/one", None, b"safe")
