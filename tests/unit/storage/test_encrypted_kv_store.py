from __future__ import annotations

import json

import pytest

from common.errors import BackendError, NotFoundError, ValidationError
from common.factory.factory import Factory
from common.security.cryptography import CryptographyProducer, CryptographyProvider
from common.security.types import CryptoContext
from common.type_def import MESSAGES_KEY_PREFIX, Scope, memory_key
from config.context import AssemblyContext
from storage.kv import KvProducer
from storage.kv_impl.encrypted_kv_store import EncryptedKVStore
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore

_PREFIX = b"fake1:"

pytestmark = pytest.mark.unit


class _FakeSecurity(CryptographyProvider):
    def __init__(self, *, allow_plaintext: bool = True) -> None:
        self.allow_plaintext = allow_plaintext
        self.fail_decrypt = False
        self.encrypt_calls: list[tuple[CryptoContext | None, bytes, bytes]] = []
        self.decrypt_calls: list[tuple[CryptoContext | None, bytes, bytes]] = []

    def encrypt(
        self,
        plaintext: bytes,
        *,
        context: CryptoContext | None = None,
        aad: bytes = b"",
    ) -> bytes:
        self.encrypt_calls.append((context, aad, plaintext))
        return _PREFIX + len(aad).to_bytes(4, "big") + aad + plaintext[::-1]

    def decrypt(
        self,
        ciphertext: bytes,
        *,
        context: CryptoContext | None = None,
        aad: bytes = b"",
    ) -> bytes:
        self.decrypt_calls.append((context, aad, ciphertext))
        if self.fail_decrypt:
            raise RuntimeError("decrypt failed")
        if not ciphertext.startswith(_PREFIX):
            if self.allow_plaintext:
                return ciphertext
            raise RuntimeError("missing encrypted envelope")
        offset = len(_PREFIX)
        aad_len = int.from_bytes(ciphertext[offset : offset + 4], "big")
        offset += 4
        embedded_aad = ciphertext[offset : offset + aad_len]
        if embedded_aad != aad:
            raise RuntimeError("aad mismatch")
        return ciphertext[offset + aad_len :][::-1]


@CryptographyProducer.register("fake_encrypted_kv")
def _build_fake_security(config):
    return _FakeSecurity(allow_plaintext=bool(config.get("allow_plaintext", True)))


def _kv(
    encryption: _FakeSecurity | None = None,
) -> tuple[EncryptedKVStore, InMemoryKVStore, _FakeSecurity]:
    raw = InMemoryKVStore()
    fake = encryption or _FakeSecurity()
    return EncryptedKVStore(raw, fake), raw, fake


def _aad_payload(aad: bytes) -> dict:
    return json.loads(aad.decode("utf-8"))


def test_encrypted_kv_store_encrypts_raw_value_and_decrypts_get() -> None:
    kv, raw, encryption = _kv()
    scope = Scope(org="acme", user="alice")
    key = memory_key("unit-1")

    kv.insert(scope, key, b"secret memory")

    raw_value = raw.get(scope, key)
    assert raw_value.startswith(_PREFIX)
    assert b"secret memory" not in raw_value
    assert kv.get(scope, key) == b"secret memory"

    context, aad, plaintext = encryption.encrypt_calls[0]
    assert plaintext == b"secret memory"
    assert context is not None
    assert context.scope == scope
    assert context.purpose == "memory_unit"
    assert context.object_id == key  # 对象标识是专有字段，不塞 metadata
    payload = _aad_payload(aad)
    assert payload["scope"]["org"] == "acme"
    assert payload["scope"]["space"] == ""
    assert payload["key"] == key
    assert payload["purpose"] == "memory_unit"


def test_encrypted_kv_store_list_decrypts_every_value_with_each_key_aad() -> None:
    kv, _, encryption = _kv()
    scope = Scope(org="acme", user="alice")

    kv.insert(scope, memory_key("u1"), b"one")
    kv.insert(scope, f"{MESSAGES_KEY_PREFIX}m1", b"two")

    listed = dict(kv.scan(scope))

    assert listed[memory_key("u1")] == b"one"
    assert listed[f"{MESSAGES_KEY_PREFIX}m1"] == b"two"
    purposes = [_aad_payload(aad)["purpose"] for _, aad, _ in encryption.decrypt_calls]
    assert purposes == ["memory_unit", "raw_message"]


def test_encrypted_kv_store_mget_decrypts_each_value_with_its_own_aad() -> None:
    # mget 委托 raw 一次性批量取密文（raw 缺失即抛 NotFoundError），再逐项解密；
    # AAD 绑定 key+purpose，各 key 的 AAD 不同（memory_unit vs raw_message），
    # 不能批量统一解密，须逐项用各自 AAD 解密。
    kv, _, security = _kv()
    scope = Scope(org="acme", user="alice")

    kv.insert(scope, memory_key("u1"), b"one")
    kv.insert(scope, f"{MESSAGES_KEY_PREFIX}m1", b"two")

    out = kv.mget(scope, [memory_key("u1"), f"{MESSAGES_KEY_PREFIX}m1"])

    assert out == [b"one", b"two"]
    purposes = [_aad_payload(aad)["purpose"] for _, aad, _ in security.decrypt_calls]
    assert purposes == ["memory_unit", "raw_message"]


def test_encrypted_kv_store_mget_missing_raises_not_found() -> None:
    # raw mget 缺失即抛 NotFoundError（与 get 一致），encrypted 不静默省略。
    kv, _, _ = _kv()
    scope = Scope(org="acme", user="alice")
    kv.insert(scope, memory_key("u1"), b"one")

    with pytest.raises(NotFoundError):
        kv.mget(scope, [memory_key("u1"), memory_key("ghost")])


def test_encrypted_kv_store_passes_through_exists_delete_and_scopes() -> None:
    kv, _, encryption = _kv()
    scope = Scope(org="acme", user="alice")
    key = "plain-key"

    kv.insert(scope, key, b"value")
    assert kv.exists(scope, key)
    assert kv.scopes() == [scope]

    kv.delete(scope, key)

    assert not kv.exists(scope, key)
    assert len(encryption.encrypt_calls) == 1
    assert not encryption.decrypt_calls


def test_encrypted_kv_store_supports_plaintext_compatibility_via_provider() -> None:
    encryption = _FakeSecurity(allow_plaintext=True)
    kv, raw, _ = _kv(encryption)
    scope = Scope(org="acme", user="alice")

    raw.insert(scope, "legacy", b"legacy plaintext")

    assert kv.get(scope, "legacy") == b"legacy plaintext"


def test_encrypted_kv_store_decryption_failure_is_fail_closed() -> None:
    kv, _, encryption = _kv()
    scope = Scope(org="acme", user="alice")
    kv.insert(scope, "key", b"value")
    encryption.fail_decrypt = True

    try:
        kv.get(scope, "key")
    except BackendError:
        return
    raise AssertionError("decrypt failure should raise BackendError")


def test_encrypted_kv_store_factory_builds_wrapper_from_named_dependencies() -> None:
    Factory.reset_all()
    ctx = AssemblyContext.from_dict(
        {
            "cryptography": {"default": "fake_encrypted_kv"},
            "kv_store": {
                "raw": "memory",
                "default": {
                    "target": "encrypted",
                    "params": {
                        "raw_kv_store": "raw",
                        "cryptography": "default",
                    },
                },
            },
        }
    )
    scope = Scope(org="acme", user="alice")

    kv = KvProducer.build_named("default", ctx)

    assert isinstance(kv, EncryptedKVStore)
    kv.insert(scope, "key", b"value")
    assert kv.get(scope, "key") == b"value"


def test_encrypted_kv_store_factory_requires_raw_dependency() -> None:
    Factory.reset_all()
    ctx = AssemblyContext.from_dict(
        {
            "cryptography": {"default": "fake_encrypted_kv"},
            "kv_store": {
                "default": {
                    "target": "encrypted",
                    "params": {"cryptography": "default"},
                }
            },
        }
    )

    try:
        KvProducer.build_named("default", ctx)
    except ValidationError:
        return
    raise AssertionError("encrypted kv should require params.raw_kv_store")
