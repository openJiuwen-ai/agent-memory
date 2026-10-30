"""Private Store isolation, transparent encryption and durable recovery contracts."""

import json
import sqlite3
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.security.cryptography import AuthenticationFailedError
from jiuwen_memory.common.security.types import CryptoContext
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.privacy import PrivacySnapshot
from jiuwen_memory.storage.privacy_impl import EncryptedPrivacyStore, SQLitePrivacyStore

pytestmark = pytest.mark.unit
SCOPE = Scope(org="org", space="space", user="user", agent="agent", session="session")
TOKEN = "[JW-PRIVATE:" + "a" * 48 + "]"
SECRET = b"private-original-13800138000"


@pytest.fixture
def stores(tmp_path, privacy_crypto):
    raw = SQLitePrivacyStore(str(tmp_path / "private.sqlite"))
    encrypted = EncryptedPrivacyStore(raw, privacy_crypto)
    yield encrypted, raw
    encrypted.close()


def write_snapshot(store, *, scope=SCOPE, revision="old", token=TOKEN, secret=SECRET):
    ref = store.stage_fragment(scope, token, secret, "S2")
    snapshot = PrivacySnapshot("memory/one", revision, "digest-" + revision, b'{"tokens":[]}')
    store.insert(scope, snapshot, {token: ref})
    return snapshot


def test_roundtrip_encrypts_before_backend_write_and_never_stages_plaintext(stores, tmp_path):
    store, raw = stores
    snapshot = write_snapshot(store)
    assert store.store_type() is StoreType.PRIVACY
    assert store.security.enabled() and store.security.encryption_binding.inner is raw
    assert raw.get(SCOPE, snapshot.unit).value != snapshot.value
    assert raw.reference(SCOPE, TOKEN).value != SECRET
    assert SECRET not in (tmp_path / "private.sqlite").read_bytes()
    assert store.get(SCOPE, snapshot.unit) == snapshot
    assert store.get_fragment(SCOPE, TOKEN) == SECRET


@pytest.mark.parametrize("dimension", ["org", "space", "user", "agent", "session"])
def test_exact_scope_isolation_for_reads_search_and_delete(stores, dimension):
    store, _ = stores
    snapshot = write_snapshot(store)
    other = replace(SCOPE, **{dimension: "other"})
    assert store.search(other, [snapshot.unit]) == []
    with pytest.raises(NotFoundError):
        store.get(other, snapshot.unit)
    with pytest.raises(NotFoundError):
        store.get_fragment(other, TOKEN)
    assert store.delete(other, None) == set()
    assert store.get_fragment(SCOPE, TOKEN) == SECRET
    assert store.scopes() == [SCOPE]


def test_crud_conflict_missing_update_and_atomic_rollback(stores):
    store, _ = stores
    old = write_snapshot(store)
    with pytest.raises(ConflictError):
        store.insert(SCOPE, replace(old, value=b"changed"), {})
    with pytest.raises(NotFoundError):
        store.update(SCOPE, replace(old, unit="missing"), {})
    new_token = "new-token"
    with pytest.raises(RuntimeError, match="delegate failed"):
        with store.transaction(SCOPE):
            ref = store.stage_fragment(SCOPE, new_token, b"new-private", "S2")
            store.update(SCOPE, replace(old, revision="new"), {new_token: ref})
            raise RuntimeError("delegate failed")
    assert store.get(SCOPE, old.unit) == old
    assert store.get_fragment(SCOPE, TOKEN) == SECRET
    with pytest.raises(NotFoundError):
        store.reference(SCOPE, new_token)


def test_recovery_survives_reopen_and_reconciles_actual_truth(stores, tmp_path, privacy_crypto):
    store, _ = stores
    old = write_snapshot(store)
    store.checkpoint(SCOPE, old.unit)
    new_token = "new-token"
    new = replace(old, revision="new", digest="new-digest")
    store.update(SCOPE, new, {new_token: store.stage_fragment(SCOPE, new_token, b"new", "S2")})
    reopened = EncryptedPrivacyStore(
        SQLitePrivacyStore(str(tmp_path / "private.sqlite")), privacy_crypto
    )
    try:
        assert reopened.search(SCOPE, [old.unit], revision="old") == [old]
        assert reopened.get_fragment(SCOPE, TOKEN) == SECRET
        reopened.reconcile(SCOPE, old.unit, old.revision, old.digest)
        assert reopened.get(SCOPE, old.unit) == old
        assert reopened.search(SCOPE, [old.unit], revision="new") == []
        with pytest.raises(NotFoundError):
            reopened.reference(SCOPE, new_token)
    finally:
        reopened.close()


def test_reference_copy_and_manifest_search_do_not_decrypt_fragments(
    stores, privacy_crypto, monkeypatch
):
    store, _ = stores
    old = write_snapshot(store)
    target = replace(SCOPE, session="target")
    decrypt = privacy_crypto.decrypt
    purposes = []

    def observe(value, **kwargs):
        purposes.append(kwargs["context"].purpose)
        return decrypt(value, **kwargs)

    monkeypatch.setattr(privacy_crypto, "decrypt", observe)
    ref = store.reference(SCOPE, TOKEN)
    store.insert(target, old, {TOKEN: ref})
    assert store.search(target, [old.unit]) == [old]
    store.delete(SCOPE, None)
    assert "privacy-fragment" not in purposes
    assert store.get_fragment(target, TOKEN) == SECRET
    assert purposes[-1] == "privacy-fragment"


def test_existing_ciphertext_context_and_sqlite_schema_remain_readable(stores, privacy_crypto):
    store, raw = stores
    snapshot = PrivacySnapshot("memory/legacy", "revision", "digest", b"legacy-manifest")
    # Produce the exact envelope contexts used before private persistence became a Store.
    manifest_cipher = privacy_crypto.encrypt(
        snapshot.value,
        context=CryptoContext(
            scope=SCOPE,
            object_id="memory/legacy:revision",
            purpose="privacy-manifest",
            format_version=1,
            metadata={"level": ""},
        ),
    )
    fragment_cipher = privacy_crypto.encrypt(
        SECRET,
        context=CryptoContext(
            scope=SCOPE,
            object_id=TOKEN,
            purpose="privacy-fragment",
            format_version=1,
            metadata={"level": "S2"},
        ),
    )
    from jiuwen_memory.storage.privacy import FragmentReference
    from jiuwen_memory.storage.privacy_impl.sqlite_privacy_store import scope_key

    raw.insert(
        SCOPE,
        replace(snapshot, value=manifest_cipher),
        {TOKEN: FragmentReference(fragment_cipher, "S2", scope_key(SCOPE))},
    )
    assert store.get(SCOPE, snapshot.unit) == snapshot
    assert store.get_fragment(SCOPE, TOKEN) == SECRET


def test_tampered_fragment_binding_fails_closed(stores, privacy_crypto):
    store, raw = stores
    old = write_snapshot(store)
    ref = store.reference(SCOPE, TOKEN)
    raw.insert(
        SCOPE,
        replace(old, unit="memory/tampered", value=b"unused"),
        {
            "wrong-token": ref,
        },
    )
    with pytest.raises(AuthenticationFailedError):
        store.get_fragment(SCOPE, "wrong-token")


def test_raw_backend_exception_is_normalized(stores, tmp_path):
    from jiuwen_memory.common.errors import BackendError

    store, _ = stores
    write_snapshot(store)
    with sqlite3.connect(tmp_path / "private.sqlite") as connection:
        connection.execute("DROP TABLE fragments")
    with pytest.raises(BackendError, match="privacy storage operation failed"):
        store.reference(SCOPE, TOKEN)


def test_snapshot_json_is_only_decrypted_at_store_boundary(stores):
    store, raw = stores
    snapshot = write_snapshot(store)
    assert json.loads(store.get(SCOPE, snapshot.unit).value) == {"tokens": []}
    with pytest.raises((ValueError, UnicodeDecodeError)):
        json.loads(raw.get(SCOPE, snapshot.unit).value)


def test_shared_store_is_closed_once_by_assembly_owner(stores, monkeypatch):
    from jiuwen_memory.common.security.capabilities import SecurityResourceOwner
    from jiuwen_memory.common.security.privacy.privacy_impl.regex import RegexPrivacy

    store, _ = stores
    consumers = (RegexPrivacy(store=store), RegexPrivacy(store=store))
    close = store.close
    closures = []

    def observe():
        closures.append("closed")
        close()

    monkeypatch.setattr(store, "close", observe)
    owner = SecurityResourceOwner(consumers)
    owner.close()
    owner.close()
    assert closures == ["closed"]
