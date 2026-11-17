# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09：加密 KV 作为归属存储时的条件操作（密文 CAS）。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.common.security.cryptography import CryptographyProvider
from jiuwen_memory.common.security.types import CryptoContext
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.capability import StoreCapability
from jiuwen_memory.storage.kv_impl.encrypted_kv_store import EncryptedKVStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")


class _FakeCrypto(CryptographyProvider):
    def encrypt(self, plaintext: bytes, *, context: CryptoContext | None = None,
                aad: bytes = b"") -> bytes:
        return b"enc:" + plaintext[::-1]

    def decrypt(self, ciphertext: bytes, *, context: CryptoContext | None = None,
                aad: bytes = b"") -> bytes:
        if not ciphertext.startswith(b"enc:"):
            raise ValueError("plaintext value")
        return ciphertext[4:][::-1]


def _store() -> EncryptedKVStore:
    return EncryptedKVStore(InMemoryKVStore(), _FakeCrypto())


def test_encrypted_store_forwards_conditional_capability() -> None:
    assert StoreCapability.CONDITIONAL_KV in _store().capabilities()


def test_encrypted_conditional_create_replace_delete() -> None:
    store = _store()
    raw = getattr(store, "_raw")
    store.create_if_absent(SCOPE, "/__shard_placement/u1", b"v1")
    assert raw.get(SCOPE, "/__shard_placement/u1").startswith(b"enc:")
    assert store.read_many(SCOPE, ["/__shard_placement/u1"]) == {
        "/__shard_placement/u1": b"v1"
    }
    with pytest.raises(ConflictError):
        store.create_if_absent(SCOPE, "/__shard_placement/u1", b"v2")

    store.replace_if_value(SCOPE, "/__shard_placement/u1", b"v1", b"v2")
    assert store.read_many(SCOPE, ["/__shard_placement/u1"])[
        "/__shard_placement/u1"
    ] == b"v2"
    with pytest.raises(ConflictError):
        store.replace_if_value(SCOPE, "/__shard_placement/u1", b"stale", b"v3")

    assert store.delete_if_value(SCOPE, "/__shard_placement/u1", b"stale") is False
    assert store.delete_if_value(SCOPE, "/__shard_placement/u1", b"v2") is True
    assert store.read_many(SCOPE, ["/__shard_placement/u1"]) == {}


def test_encrypted_now_and_scan_entries() -> None:
    store = _store()
    store.create_if_absent(SCOPE, "k1", b"a")
    store.create_if_absent(SCOPE, "k2", b"b")
    assert store.now() > 0
    assert dict(store.scan_entries(SCOPE, "k")) == {"k1": b"a", "k2": b"b"}
