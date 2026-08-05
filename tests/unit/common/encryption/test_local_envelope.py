from __future__ import annotations

import stat

import pytest

import common.encryption.encryption_impl
from common.encryption import (
    AuthenticationFailedError,
    CorruptedCiphertextError,
    EncryptionContext,
    EncryptionProducer,
    InvalidMagicError,
    KeyMismatchError,
)
from common.encryption.encryption_impl.local_envelope import (
    ENVELOPE_MAGIC,
    LocalEnvelopeEncryptionProvider,
    LocalKeyProvider,
)
from common.factory.factory import Factory
from common.type_def import Scope
from config import AssemblyContext

_KEY_HEX = "11" * 32

pytestmark = pytest.mark.unit


def _context(
    *,
    org: str = "acme",
    user: str = "alice",
    purpose: str = "memory_unit",
) -> EncryptionContext:
    return EncryptionContext(
        scope=Scope(org=org, user=user),
        purpose=purpose,
        metadata={"key": "/memory/u1"},
    )


def _provider_from_hex(*, allow_plaintext: bool = True) -> LocalEnvelopeEncryptionProvider:
    return LocalEnvelopeEncryptionProvider(
        LocalKeyProvider(key_hex=_KEY_HEX),
        allow_plaintext=allow_plaintext,
    )


def test_local_security_provider_encrypts_enc1_and_round_trips(tmp_path) -> None:
    key_file = tmp_path / "master.key"
    provider = LocalEnvelopeEncryptionProvider(LocalKeyProvider(key_file=str(key_file)))
    context = _context()

    ciphertext = provider.encrypt(b"secret payload", context=context, aad=b"kv:a")
    second_ciphertext = provider.encrypt(b"secret payload", context=context, aad=b"kv:a")

    assert ciphertext.startswith(ENVELOPE_MAGIC)
    assert ciphertext != b"secret payload"
    assert ciphertext != second_ciphertext
    assert provider.decrypt(ciphertext, context=context, aad=b"kv:a") == b"secret payload"
    assert key_file.exists()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_local_security_provider_supports_plaintext_compatibility() -> None:
    provider = _provider_from_hex()

    assert provider.decrypt(b"legacy plaintext", context=_context()) == b"legacy plaintext"


def test_local_security_provider_can_reject_plaintext_in_strict_mode() -> None:
    provider = _provider_from_hex(allow_plaintext=False)

    with pytest.raises(InvalidMagicError):
        provider.decrypt(b"legacy plaintext", context=_context())


def test_local_security_provider_defaults_to_strict_not_plaintext() -> None:
    """审计 P2-3：默认 fail-closed，不静默放行明文。

    默认 True 时，拥有底层存储写权限的攻击者可用任意明文替换密文，绕过 AES-GCM
    tag 与 AAD。迁移期读旧明文须显式 opt-in（allow_plaintext=true）。
    """
    provider = LocalEnvelopeEncryptionProvider(LocalKeyProvider(key_hex=_KEY_HEX))

    with pytest.raises(InvalidMagicError):
        provider.decrypt(b"legacy plaintext", context=_context())


def test_local_security_provider_rejects_aad_or_context_mismatch() -> None:
    provider = _provider_from_hex()
    ciphertext = provider.encrypt(b"secret payload", context=_context(user="alice"), aad=b"kv:a")

    with pytest.raises(AuthenticationFailedError):
        provider.decrypt(ciphertext, context=_context(user="alice"), aad=b"kv:b")
    with pytest.raises(AuthenticationFailedError):
        provider.decrypt(ciphertext, context=_context(user="bob"), aad=b"kv:a")


def test_local_security_provider_rejects_org_key_mismatch() -> None:
    provider = _provider_from_hex()
    ciphertext = provider.encrypt(b"secret payload", context=_context(org="acme"), aad=b"kv:a")

    with pytest.raises(KeyMismatchError):
        provider.decrypt(ciphertext, context=_context(org="other"), aad=b"kv:a")


def test_local_security_provider_rejects_corrupted_envelope() -> None:
    provider = _provider_from_hex()

    with pytest.raises(CorruptedCiphertextError):
        provider.decrypt(ENVELOPE_MAGIC, context=_context())


def test_security_producer_builds_local_provider_from_config(tmp_path) -> None:
    assert common.encryption.encryption_impl.EncryptionProducer is EncryptionProducer
    Factory.reset_all()
    key_file = tmp_path / "configured.key"
    ctx = AssemblyContext.from_dict(
        {
            "encryption": {
                "default": {
                    "target": "local",
                    "params": {"key_file": str(key_file)},
                }
            }
        }
    )

    provider = EncryptionProducer.build_named("default", ctx)
    context = _context()
    ciphertext = provider.encrypt(b"value", context=context, aad=b"kv:a")

    assert isinstance(provider, LocalEnvelopeEncryptionProvider)
    assert provider.decrypt(ciphertext, context=context, aad=b"kv:a") == b"value"
    assert key_file.exists()
