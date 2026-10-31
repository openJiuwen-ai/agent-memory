"""Real-provider FS contracts: ciphertext, stable identity, bounds and fail-closed reads."""

from __future__ import annotations

import io
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
    LocalEnvelopeCryptographyProvider,
    LocalKeyProvider,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.fs import FsProducer, FSStore
from jiuwen_memory.storage.fs_impl.encrypted_fs_store import EncryptedFSStore
from jiuwen_memory.storage.fs_impl.in_memory_fs_store import InMemoryFSStore
from jiuwen_memory.storage.fs_impl.local_fs import LocalFSStore

pytestmark = pytest.mark.unit
SCOPE = Scope(org="acme", space="product", user="alice", agent="bot", session="s1")


def _crypto(key="11" * 32):
    return LocalEnvelopeCryptographyProvider(LocalKeyProvider(key_file="", key_env="", key_hex=key))


@pytest.fixture(params=["memory", "local"])
def pair(request, tmp_path):
    inner = InMemoryFSStore() if request.param == "memory" else LocalFSStore(root=str(tmp_path))
    return EncryptedFSStore(inner, _crypto()), inner


@pytest.mark.parametrize("data", [b"", b"secret payload", bytes(range(256))])
def test_real_provider_roundtrip_and_persisted_ciphertext(pair, data):
    fs, inner = pair
    ref = fs.insert(SCOPE, "sample.bin", io.BytesIO(data))
    with inner.get(SCOPE, ref) as stream:
        raw = stream.read()
    assert raw.startswith(b"ENC1")
    assert not data or data not in raw
    with fs.get(SCOPE, ref) as stream:
        assert stream.read() == data
    assert fs.security.enabled()
    fs.security.health()
    fs.health()
    assert fs.stat(SCOPE, ref).size == len(raw)


def test_local_aliases_share_object_identity(tmp_path):
    inner = LocalFSStore(root=str(tmp_path))
    fs = EncryptedFSStore(inner, _crypto())
    ref = fs.insert(SCOPE, "a/../sample.bin", io.BytesIO(b"secret"))
    for alias in (ref, "./sample.bin", "sample.bin"):
        with fs.get(SCOPE, alias) as stream:
            assert stream.read() == b"secret"
    fs.update(SCOPE, "./sample.bin", io.BytesIO(b"new"))
    with fs.get(SCOPE, ref) as stream:
        assert stream.read() == b"new"


def test_crud_semantics_and_input_stream_ownership(pair):
    fs, _ = pair
    source = io.BytesIO(b"old")
    ref = fs.insert(SCOPE, "sample.bin", source)
    assert not source.closed
    with pytest.raises(ConflictError):
        fs.insert(SCOPE, "sample.bin", io.BytesIO(b"other"))
    fs.update(SCOPE, ref, io.BytesIO(b"new"))
    with fs.get(SCOPE, ref) as stream:
        assert stream.read() == b"new"
    fs.delete(SCOPE, ref)
    fs.delete(SCOPE, ref)
    with pytest.raises(NotFoundError):
        fs.get(SCOPE, ref)
    with pytest.raises(NotFoundError):
        fs.update(SCOPE, ref, io.BytesIO(b"new"))


@pytest.mark.parametrize("dimension", ["org", "space", "user", "agent", "session", "ref"])
def test_ciphertext_cannot_move_between_objects_or_scope_dimensions(pair, dimension):
    fs, inner = pair
    ref = fs.insert(SCOPE, "sample.bin", io.BytesIO(b"secret"))
    target = SCOPE if dimension == "ref" else replace(SCOPE, **{dimension: "other"})
    with inner.get(SCOPE, ref) as stream:
        moved = inner.insert(target, "other.bin" if dimension == "ref" else "sample.bin", stream)
    with pytest.raises(BackendError):
        fs.get(target, moved)


@pytest.mark.parametrize("damage", ["plaintext", "tamper", "truncate", "wrong_key"])
def test_invalid_ciphertext_never_returns_plaintext(pair, damage):
    fs, inner = pair
    ref = fs.insert(SCOPE, "sample.bin", io.BytesIO(b"secret"))
    with inner.get(SCOPE, ref) as stream:
        raw = stream.read()
    if damage == "wrong_key":
        fs = EncryptedFSStore(inner, _crypto("22" * 32))
    else:
        raw = {
            "plaintext": b"old plaintext",
            "tamper": raw[:-1] + bytes([raw[-1] ^ 1]),
            "truncate": raw[:8],
        }[damage]
        inner.update(SCOPE, ref, io.BytesIO(raw))
    with pytest.raises(BackendError):
        fs.get(SCOPE, ref)


class _ShortStream(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 1) if size >= 0 else size)


def test_short_reads_and_exact_plaintext_boundary(pair):
    _, inner = pair
    fs = EncryptedFSStore(inner, _crypto(), max_plaintext_bytes=6)
    ref = fs.insert(SCOPE, "sample.bin", _ShortStream(b"abcdef"))
    with fs.get(SCOPE, ref) as stream:
        assert stream.read() == b"abcdef"
    with pytest.raises(ValidationError):
        fs.insert(SCOPE, "big.bin", _ShortStream(b"abcdefg"))
    with pytest.raises(ValidationError):
        fs.update(SCOPE, ref, _ShortStream(b"abcdefg"))
    with fs.get(SCOPE, ref) as stream:
        assert stream.read() == b"abcdef"


def test_ciphertext_write_limit_rejects_before_creating_object(pair):
    _, inner = pair
    fs = EncryptedFSStore(inner, _crypto(), max_ciphertext_bytes=8)
    with pytest.raises(ValidationError):
        fs.insert(SCOPE, "sample.bin", io.BytesIO(b"abc"))
    ref = inner.object_id(SCOPE, "sample.bin", for_insert=True)
    with pytest.raises(NotFoundError):
        inner.stat(SCOPE, ref)


def test_toctou_ciphertext_read_is_bounded_and_stream_closed(pair, monkeypatch):
    fs, inner = pair
    ref = fs.insert(SCOPE, "sample.bin", io.BytesIO(b"secret"))
    fs = EncryptedFSStore(inner, _crypto(), max_ciphertext_bytes=256)
    source = io.BytesIO(b"x" * 1024)
    monkeypatch.setattr(inner, "get", lambda *args: source)
    with pytest.raises(ValidationError):
        fs.get(SCOPE, ref)
    assert source.closed


def test_read_rechecks_plaintext_limit(pair):
    fs, inner = pair
    ref = fs.insert(SCOPE, "sample.bin", io.BytesIO(b"secret"))
    limited = EncryptedFSStore(inner, _crypto(), max_plaintext_bytes=3)
    with pytest.raises(ValidationError):
        limited.get(SCOPE, ref)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, None, "invalid"])
def test_invalid_limits_are_rejected(value):
    with pytest.raises(ValidationError):
        EncryptedFSStore(InMemoryFSStore(), _crypto(), max_plaintext_bytes=value)


def test_backend_without_stable_identity_is_rejected():
    class UnstableFS(InMemoryFSStore):
        object_id = FSStore.object_id

    with pytest.raises(ValidationError):
        EncryptedFSStore(UnstableFS(), _crypto())


def test_factory_registration_and_shared_crypto():
    from jiuwen_memory.common.security.cryptography import CryptographyProducer

    Factory.reset_all()
    register_backends()
    ctx = AssemblyContext.from_dict(
        {
            "key_provider": {"default": {"target": "local", "params": {"key_hex": "11" * 32}}},
            "cryptography": {"default": {"target": "local", "params": {"key_provider": "default"}}},
            "fs_store": {
                "default": {
                    "target": "encrypted",
                    "params": {
                        "inner": {"target": "memory"},
                        "cryptography": "default",
                    },
                }
            },
        }
    )
    store = FsProducer.build_named("default", ctx)
    assert store.security.encryption_binding.provider is CryptographyProducer.build_named(
        "default", ctx
    )
    ref = store.insert(SCOPE, "sample.bin", io.BytesIO(b"secret"))
    with store.get(SCOPE, ref) as stream:
        assert stream.read() == b"secret"


@pytest.mark.parametrize("inner", [None, "default"])
def test_factory_rejects_missing_or_self_referencing_dependency(inner):
    Factory.reset_all()
    ctx = AssemblyContext.from_dict(
        {
            "fs_store": {
                "default": {
                    "target": "encrypted",
                    "params": {"inner": inner},
                }
            }
        }
    )
    with pytest.raises(ValidationError):
        FsProducer.build_named("default", ctx)
