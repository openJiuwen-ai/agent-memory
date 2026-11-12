"""Private state over selectable KV backends: atomicity, recovery and ownership."""

import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.security.capabilities import SecurityResourceOwner
from jiuwen_memory.storage.kv_impl.encrypted_kv_store import EncryptedKVStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.redis_kv import RedisKVStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
from jiuwen_memory.storage.privacy_impl import EncryptedPrivacyStore, KVPrivacyStore
from jiuwen_memory.storage.privacy_impl.kv_privacy_store import STATE_KEY
from tests.unit.storage import test_privacy_store as contract

pytestmark = pytest.mark.unit
SCOPE, TOKEN, SECRET = contract.SCOPE, contract.TOKEN, contract.SECRET


@pytest.fixture(params=["memory", "sqlite"])
def backend(request, tmp_path):
    kv = (
        InMemoryKVStore()
        if request.param == "memory"
        else SQLiteKVStore(str(tmp_path / "private-kv.sqlite"))
    )
    yield kv
    if hasattr(kv, "close"):
        kv.close()


@pytest.fixture
def stores(backend, privacy_crypto):
    raw = KVPrivacyStore(backend)
    return EncryptedPrivacyStore(raw, privacy_crypto), raw


def test_encrypted_roundtrip(stores, backend):
    store, raw = stores
    snapshot = contract.write_snapshot(store)
    assert store.get(SCOPE, snapshot.unit) == snapshot
    assert store.get_fragment(SCOPE, TOKEN) == SECRET
    assert raw.get(SCOPE, snapshot.unit).value != snapshot.value
    assert SECRET not in backend.get(SCOPE, STATE_KEY)
    assert snapshot.value not in backend.get(SCOPE, STATE_KEY)


@pytest.mark.parametrize("dimension", ["org", "space", "user", "agent", "session"])
def test_scope_isolation(stores, dimension):
    contract.test_exact_scope_isolation_for_reads_search_and_delete(stores, dimension)


def test_rollback_and_crud(stores):
    contract.test_crud_conflict_missing_update_and_atomic_rollback(stores)


def test_reference_copy_does_not_decrypt(stores, privacy_crypto, monkeypatch):
    contract.test_reference_copy_and_manifest_search_do_not_decrypt_fragments(
        stores, privacy_crypto, monkeypatch
    )


def test_recovery_after_reopen(tmp_path, privacy_crypto):
    path = str(tmp_path / "state.sqlite")
    first = EncryptedPrivacyStore(KVPrivacyStore(SQLiteKVStore(path), owns_kv=True), privacy_crypto)
    old = contract.write_snapshot(first)
    first.checkpoint(SCOPE, old.unit)
    first.update(SCOPE, replace(old, revision="new", digest="new"), {})
    first.close()
    second = EncryptedPrivacyStore(
        KVPrivacyStore(SQLiteKVStore(path), owns_kv=True), privacy_crypto
    )
    try:
        assert second.search(SCOPE, [old.unit], revision="old") == [old]
        second.reconcile(SCOPE, old.unit, "unknown", "unknown")
        assert second.get_fragment(SCOPE, TOKEN) == SECRET
        second.reconcile(SCOPE, old.unit, old.revision, old.digest)
        assert second.get(SCOPE, old.unit) == old
        assert second.search(SCOPE, [old.unit], revision="new") == []
        second.delete(SCOPE, None)
        assert second.scopes() == []
    finally:
        second.close()


def test_nested_savepoint_preserves_outer_write(stores):
    store, _ = stores
    with store.transaction(SCOPE):
        snapshot = contract.write_snapshot(store)
        with pytest.raises(RuntimeError), store.transaction(SCOPE):
            store.delete(SCOPE, None)
            raise RuntimeError("abort nested operation")
        assert store.get(SCOPE, snapshot.unit) == snapshot
    assert store.get_fragment(SCOPE, TOKEN) == SECRET


def test_two_adapters_reject_stale_publication_without_losing_winner(backend, privacy_crypto):
    one = EncryptedPrivacyStore(KVPrivacyStore(backend), privacy_crypto)
    two = EncryptedPrivacyStore(KVPrivacyStore(backend), privacy_crypto)
    with pytest.raises(ConflictError), one.transaction(SCOPE):
        contract.write_snapshot(one)
        winner = replace(contract.write_snapshot(two), unit="memory/winner")
        two.insert(SCOPE, winner, {TOKEN: two.reference(SCOPE, TOKEN)})
    assert two.get(SCOPE, winner.unit) == winner
    assert two.get_fragment(SCOPE, TOKEN) == SECRET


def test_limit_corruption_and_unsupported_backends_fail_closed(backend):
    raw = KVPrivacyStore(backend, max_state_bytes=5)
    with pytest.raises(ValidationError, match="limit"):
        contract.write_snapshot(raw)
    assert not backend.exists(SCOPE, STATE_KEY)
    backend.insert(SCOPE, STATE_KEY, b'{"version":true}')
    with pytest.raises(BackendError):
        KVPrivacyStore(backend).get(SCOPE, "memory/one")
    with pytest.raises(ValidationError, match="compare_and_swap"):

        class Unsupported(InMemoryKVStore):
            @staticmethod
            def supports_compare_and_swap():
                return False

        KVPrivacyStore(Unsupported())


def test_applied_write_with_lost_ack_preserves_recovery(backend, privacy_crypto, monkeypatch):
    store = EncryptedPrivacyStore(KVPrivacyStore(backend), privacy_crypto)
    old = contract.write_snapshot(store)
    store.checkpoint(SCOPE, old.unit)
    swap = backend.compare_and_swap

    def lost_ack(*args):
        assert swap(*args)
        raise BackendError("write acknowledgement lost")

    monkeypatch.setattr(backend, "compare_and_swap", lost_ack)
    with pytest.raises(BackendError):
        store.update(SCOPE, replace(old, revision="new", digest="new"), {})
    monkeypatch.setattr(backend, "compare_and_swap", swap)
    store.reconcile(SCOPE, old.unit, old.revision, old.digest)
    assert store.get(SCOPE, old.unit) == old
    assert store.get_fragment(SCOPE, TOKEN) == SECRET


def test_owned_encrypted_kv_closes_backend_once_and_borrowed_stays_open(
    tmp_path, privacy_crypto, monkeypatch
):
    kv = SQLiteKVStore(str(tmp_path / "owned.sqlite"))
    closes = []
    original = kv.close

    def close():
        closes.append(True)
        original()

    monkeypatch.setattr(kv, "close", close)
    borrowed = KVPrivacyStore(kv)
    borrowed.close()
    assert closes == []
    store = EncryptedPrivacyStore(
        KVPrivacyStore(EncryptedKVStore(kv, privacy_crypto), owns_kv=True), privacy_crypto
    )
    owner = SecurityResourceOwner((store,))
    owner.close()
    owner.close()
    assert closes == [True]


def test_delete_collects_only_unreferenced_fragments(stores):
    store, _ = stores
    old = contract.write_snapshot(store)
    other = replace(old, unit="memory/two")
    store.insert(SCOPE, other, {TOKEN: store.reference(SCOPE, TOKEN)})
    store.delete(SCOPE, [old.unit])
    assert store.get_fragment(SCOPE, TOKEN) == SECRET
    store.delete(SCOPE, [other.unit])
    with pytest.raises(NotFoundError):
        store.get_fragment(SCOPE, TOKEN)


def test_owned_redis_client_is_released_once(monkeypatch):
    closes = []

    class Client:
        @staticmethod
        def close():
            closes.append(True)

    # Client construction stays lazy; inject the public client's factory.
    factory = SimpleNamespace(from_url=lambda *args, **kwargs: Client())
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=factory))
    kv = RedisKVStore(url="redis://localhost:6379")
    assert isinstance(kv.client, Client)
    store = KVPrivacyStore(kv, owns_kv=True)
    store.close()
    store.close()
    assert closes == [True]
