from __future__ import annotations

import stat

import pytest

import common.security.security_impl
from common.errors import BackendError, ValidationError
from common.factory.factory import Factory
from common.security import (
    AuthenticationFailedError,
    CorruptedCiphertextError,
    InvalidMagicError,
    KeyMismatchError,
    SecurityContext,
    SecurityProducer,
)
from common.security.security_impl.local_envelope_security_provider import (
    ENVELOPE_MAGIC,
    LocalEnvelopeSecurityProvider,
    LocalKeyProvider,
)
from common.type_def import Scope
from config import AssemblyContext

_KEY_HEX = "11" * 32

pytestmark = pytest.mark.unit


def _context(
    *,
    org: str = "acme",
    user: str = "alice",
    purpose: str = "memory_unit",
) -> SecurityContext:
    return SecurityContext(
        scope=Scope(org=org, user=user),
        purpose=purpose,
        metadata={"key": "/memory/u1"},
    )


def _provider_from_hex(*, allow_plaintext: bool = True) -> LocalEnvelopeSecurityProvider:
    return LocalEnvelopeSecurityProvider(
        LocalKeyProvider(key_hex=_KEY_HEX),
        allow_plaintext=allow_plaintext,
    )


def test_local_security_provider_encrypts_enc1_and_round_trips(tmp_path) -> None:
    key_file = tmp_path / "master.key"
    provider = LocalEnvelopeSecurityProvider(
        LocalKeyProvider(key_file=str(key_file), create_key_file=True)
    )
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
    assert common.security.security_impl.SecurityProducer is SecurityProducer
    Factory.reset_all()
    key_file = tmp_path / "configured.key"
    ctx = AssemblyContext.from_dict(
        {
            "security": {
                "default": {
                    "target": "local",
                    "params": {"key_file": str(key_file), "create_key_file": True},
                }
            }
        }
    )

    provider = SecurityProducer.build_named("default", ctx)
    context = _context()
    ciphertext = provider.encrypt(b"value", context=context, aad=b"kv:a")

    assert isinstance(provider, LocalEnvelopeSecurityProvider)
    assert provider.decrypt(ciphertext, context=context, aad=b"kv:a") == b"value"
    assert key_file.exists()


def test_local_security_missing_key_source_raises_at_init(
    monkeypatch, tmp_path
) -> None:
    """create_key_file=False + 无 key 源 + key_file 不存在 → 装配即 fail-closed。"""
    monkeypatch.delenv("AGENT_MEMORY_ENCRYPTION_ROOT_KEY", raising=False)
    nonexistent = tmp_path / "missing.key"

    with pytest.raises(BackendError) as exc_info:
        LocalEnvelopeSecurityProvider(
            LocalKeyProvider(key_file=str(nonexistent), create_key_file=False)
        )

    msg = str(exc_info.value)
    assert "no encryption root key source" in msg
    assert "key_hex" in msg and "key_b64" in msg and "key_env" in msg
    assert "create_key_file=true" in msg
    assert "openssl rand -hex 32" in msg
    assert not nonexistent.exists()


def test_local_security_invalid_key_hex_raises_at_init() -> None:
    """配了 key_hex 但解码失败 → 装配期即抛 ValidationError。"""
    with pytest.raises(ValidationError) as exc_info:
        LocalEnvelopeSecurityProvider(LocalKeyProvider(key_hex="not-valid-hex!"))
    msg = str(exc_info.value)
    assert "invalid hex encryption root key" in msg
    assert "key_hex" in msg


def test_local_security_invalid_key_b64_raises_at_init() -> None:
    """配了 key_b64 但解码失败 → 装配期即抛 ValidationError。"""
    with pytest.raises(ValidationError) as exc_info:
        LocalEnvelopeSecurityProvider(LocalKeyProvider(key_b64="!!!not-base64!!!"))
    msg = str(exc_info.value)
    assert "invalid base64 encryption root key" in msg
    assert "key_b64" in msg


def test_local_security_invalid_env_raises_at_init(monkeypatch) -> None:
    """配了 key_env 但 env 值解码失败 → 装配期即抛 ValidationError。"""
    monkeypatch.setenv("AGENT_MEMORY_ENCRYPTION_ROOT_KEY", "garbage-not-hex!")
    with pytest.raises(ValidationError) as exc_info:
        LocalEnvelopeSecurityProvider(LocalKeyProvider(key_env="AGENT_MEMORY_ENCRYPTION_ROOT_KEY"))
    msg = str(exc_info.value)
    assert "invalid hex encryption root key" in msg
    assert "AGENT_MEMORY_ENCRYPTION_ROOT_KEY" in msg


def test_local_security_create_key_file_enabled_still_works(tmp_path) -> None:
    """dev escape hatch：create_key_file=True 时仍自动生成 master.key。"""
    key_file = tmp_path / "auto.key"
    provider = LocalEnvelopeSecurityProvider(
        LocalKeyProvider(key_file=str(key_file), create_key_file=True)
    )

    ciphertext = provider.encrypt(b"payload", context=_context(), aad=b"kv:a")

    assert ciphertext.startswith(ENVELOPE_MAGIC)
    assert provider.decrypt(ciphertext, context=_context(), aad=b"kv:a") == b"payload"
    assert key_file.exists()
