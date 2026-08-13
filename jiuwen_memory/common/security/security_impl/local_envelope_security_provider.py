"""Local ENC1 SecurityProvider implementation.

This module keeps the cryptographic implementation in common.security, away from
storage decorators. It uses envelope encryption:

root key -> HKDF(org) -> org key -> AES-GCM wraps per-value data key
data key -> AES-GCM encrypts the value bytes
"""

from __future__ import annotations

import binascii
import json
import os
import secrets
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security import (
    AuthenticationFailedError,
    CorruptedCiphertextError,
    InvalidMagicError,
    KeyMismatchError,
    SecurityContext,
    SecurityProducer,
    SecurityProvider,
)

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except ImportError as import_error:  # pragma: no cover - exercised only in minimal installs
    _CRYPTO_IMPORT_ERROR: ImportError | None = import_error
    InvalidTag = None  # type: ignore[assignment]
    AESGCM = None  # type: ignore[assignment]
    HKDF = None  # type: ignore[assignment]
    hashes = None  # type: ignore[assignment]
else:
    _CRYPTO_IMPORT_ERROR = None


ENVELOPE_MAGIC = b"ENC1"
ENVELOPE_VERSION = 0x01
LOCAL_PROVIDER_ID = 0x01
NONCE_SIZE = 12
DATA_KEY_SIZE = 32
_HEADER = struct.Struct("!4sBBHHH")
_DEFAULT_KEY_FILE = "~/.agent-memory/security/master.key"
_DEFAULT_KEY_ENV = "AGENT_MEMORY_ENCRYPTION_ROOT_KEY"
_HKDF_SALT = b"agent-memory-security-local-salt-v1"


@dataclass(frozen=True)
class _Envelope:
    provider_id: int
    encrypted_data_key: bytes
    key_nonce: bytes
    data_nonce: bytes
    encrypted_content: bytes


def _private_file_opener(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


class LocalKeyProvider:
    """Local root-key provider for single-node or development deployments."""

    provider_id = LOCAL_PROVIDER_ID

    def __init__(
        self,
        *,
        key_file: str = _DEFAULT_KEY_FILE,
        key_hex: str = "",
        key_b64: str = "",
        key_env: str = _DEFAULT_KEY_ENV,
        create_key_file: bool = False,
    ) -> None:
        _ensure_crypto()
        self._key_file = Path(key_file).expanduser() if key_file else None
        self._key_hex = key_hex.strip()
        self._key_b64 = key_b64.strip()
        self._key_env = key_env.strip()
        self._create_key_file_enabled = create_key_file
        self._root_key: bytes | None = None

    def get_encryption_root_key(self) -> bytes:
        if self._root_key is None:
            self._root_key = self._load_or_create_root_key()
        return self._root_key

    def derive_org_key(self, org_id: str) -> bytes:
        root_key = self.get_encryption_root_key()
        hkdf_type = HKDF
        hashes_module = hashes
        if hkdf_type is None or hashes_module is None:
            _ensure_crypto()
            raise BackendError("cryptography HKDF support is unavailable")
        hkdf = hkdf_type(
            algorithm=hashes_module.SHA256(),
            length=DATA_KEY_SIZE,
            salt=_HKDF_SALT,
            info=b"agent-memory:security:kek:v1:" + org_id.encode("utf-8"),
        )
        return hkdf.derive(root_key)

    def encrypt_key(self, plaintext: bytes, org_id: str) -> tuple[bytes, bytes]:
        if len(plaintext) != DATA_KEY_SIZE:
            raise ValidationError("local security data key must be 32 bytes")
        org_key = self.derive_org_key(org_id)
        nonce = secrets.token_bytes(NONCE_SIZE)
        return _aes_encrypt(org_key, nonce, plaintext, _key_aad(org_id)), nonce

    def decrypt_key(self, ciphertext: bytes, nonce: bytes, org_id: str) -> bytes:
        org_key = self.derive_org_key(org_id)
        try:
            data_key = _aes_decrypt(org_key, nonce, ciphertext, _key_aad(org_id))
        except AuthenticationFailedError as exc:
            raise KeyMismatchError("encrypted data key cannot be decrypted") from exc
        if len(data_key) != DATA_KEY_SIZE:
            raise CorruptedCiphertextError("decrypted data key has invalid length")
        return data_key

    def _load_or_create_root_key(self) -> bytes:
        if self._key_hex:
            return _decode_hex_key(self._key_hex, source="key_hex")
        if self._key_b64:
            return _decode_b64_key(self._key_b64, source="key_b64")

        env_value = os.environ.get(self._key_env) if self._key_env else None
        if env_value:
            return _decode_key_string(env_value, source=f"env {self._key_env}")

        if self._key_file is None:
            raise self._missing_key_error()
        if self._key_file.exists():
            _restrict_file_mode(self._key_file)
            return _decode_hex_key(
                self._key_file.read_text(encoding="ascii").strip(),
                source=str(self._key_file),
            )
        if not self._create_key_file_enabled:
            raise self._missing_key_error()
        return self._create_key_file()

    def validate_key_source_or_raise(self) -> None:
        """装配期预检：密钥源缺失或解码失败时立即 fail-closed（F04 §5）。

        尝试解码已配置的 ``key_hex``/``key_b64``/``key_env``——解码失败抛
        :class:`ValidationError`（携带源标识）。``key_file`` 内容的解码推迟到
        """
        if self._root_key is not None:
            return
        if self._key_hex:
            _decode_hex_key(self._key_hex, source="key_hex")
            return
        if self._key_b64:
            _decode_b64_key(self._key_b64, source="key_b64")
            return
        env_value = os.environ.get(self._key_env) if self._key_env else None
        if env_value:
            _decode_key_string(env_value, source=f"env {self._key_env}")
            return
        if self._key_file and self._key_file.exists():
            return
        if self._create_key_file_enabled:
            return
        raise self._missing_key_error()

    def _missing_key_error(self) -> BackendError:
        sources: list[str] = []
        if self._key_env:
            sources.append(f"env {self._key_env} (unset)")
        if self._key_file:
            sources.append(f"key_file={self._key_file} (does not exist)")

        checked = ", ".join(sources) if sources else "no source configured"
        return BackendError(
            "local security provider has no encryption root key source. "
            f"Checked: {checked}. Provide one of: "
            "(1) security.default.params.key_hex=<64-hex-chars>; "
            "(2) security.default.params.key_b64=<base64-32-bytes>; "
            "(3) security.default.params.key_env=<ENV_VAR_NAME> and set the env var; "
            "(4) security.default.params.key_file=<path> to existing file; "
            "(5) security.default.params.create_key_file=true for dev/single-node auto-generation. "
            "Generate a key with: openssl rand -hex 32. "
            "Refusing to assemble: encryption cannot proceed without a key (fail-closed per F04 §5)."
        )

    def _create_key_file(self) -> bytes:
        key_file = self._key_file
        if key_file is None:
            raise BackendError("local security key file is not configured")
        key = secrets.token_bytes(DATA_KEY_SIZE)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(
                key_file,
                "x",
                encoding="ascii",
                opener=_private_file_opener,
            ) as key_stream:
                key_stream.write(f"{key.hex()}\n")
        except FileExistsError:
            _restrict_file_mode(key_file)
            return _decode_hex_key(
                key_file.read_text(encoding="ascii").strip(),
                source=str(key_file),
            )
        except Exception:
            key_file.unlink(missing_ok=True)
            raise
        _restrict_file_mode(key_file)
        return key


class LocalEnvelopeSecurityProvider(SecurityProvider):
    """ENC1 AES-256-GCM provider using a local encryption root key."""

    def __init__(
        self,
        key_provider: LocalKeyProvider,
        *,
        allow_plaintext: bool = True,
    ) -> None:
        _ensure_crypto()
        self._key_provider = key_provider
        self._allow_plaintext = allow_plaintext
        # 装配期 fail-fast：未配置任何密钥源时立即抛错（F04 §5 fail-closed）。
        key_provider.validate_key_source_or_raise()

    def encrypt(
        self,
        plaintext: bytes,
        *,
        context: SecurityContext | None = None,
        aad: bytes = b"",
    ) -> bytes:
        data_key = secrets.token_bytes(DATA_KEY_SIZE)
        data_nonce = secrets.token_bytes(NONCE_SIZE)
        org_id = _org_id(context)
        encrypted_content = _aes_encrypt(
            data_key,
            data_nonce,
            plaintext,
            _effective_aad(context, aad),
        )
        encrypted_data_key, key_nonce = self._key_provider.encrypt_key(data_key, org_id)
        return _build_envelope(
            LOCAL_PROVIDER_ID,
            encrypted_data_key,
            key_nonce,
            data_nonce,
            encrypted_content,
        )

    def decrypt(
        self,
        ciphertext: bytes,
        *,
        context: SecurityContext | None = None,
        aad: bytes = b"",
    ) -> bytes:
        if not ciphertext.startswith(ENVELOPE_MAGIC):
            if self._allow_plaintext:
                return ciphertext
            raise InvalidMagicError("ciphertext is not an ENC1 envelope")

        envelope = _parse_envelope(ciphertext)
        _validate_local_envelope(envelope)
        data_key = self._key_provider.decrypt_key(
            envelope.encrypted_data_key,
            envelope.key_nonce,
            _org_id(context),
        )
        return _aes_decrypt(
            data_key,
            envelope.data_nonce,
            envelope.encrypted_content,
            _effective_aad(context, aad),
        )

    def health(self) -> None:
        self._key_provider.get_encryption_root_key()


def _ensure_crypto() -> None:
    if _CRYPTO_IMPORT_ERROR is not None:
        raise BackendError(
            "security.local requires the 'cryptography' package; install project dependencies"
        ) from _CRYPTO_IMPORT_ERROR


def _build_envelope(
    provider_id: int,
    encrypted_data_key: bytes,
    key_nonce: bytes,
    data_nonce: bytes,
    encrypted_content: bytes,
) -> bytes:
    header = _HEADER.pack(
        ENVELOPE_MAGIC,
        ENVELOPE_VERSION,
        provider_id,
        len(encrypted_data_key),
        len(key_nonce),
        len(data_nonce),
    )
    return header + encrypted_data_key + key_nonce + data_nonce + encrypted_content


def _parse_envelope(ciphertext: bytes) -> _Envelope:
    if len(ciphertext) < _HEADER.size:
        raise CorruptedCiphertextError("ENC1 envelope too short")
    magic, version, provider_id, key_len, key_nonce_len, data_nonce_len = _HEADER.unpack(
        ciphertext[: _HEADER.size]
    )
    if magic != ENVELOPE_MAGIC:
        raise InvalidMagicError("ciphertext is not an ENC1 envelope")
    if version != ENVELOPE_VERSION:
        raise CorruptedCiphertextError(f"unsupported ENC1 version: {version}")

    offset = _HEADER.size
    body_len = key_len + key_nonce_len + data_nonce_len
    if len(ciphertext) < offset + body_len:
        raise CorruptedCiphertextError("ENC1 envelope length is incomplete")

    encrypted_key = ciphertext[offset: offset + key_len]
    offset += key_len
    key_nonce = ciphertext[offset: offset + key_nonce_len]
    offset += key_nonce_len
    data_nonce = ciphertext[offset: offset + data_nonce_len]
    offset += data_nonce_len
    encrypted_content = ciphertext[offset:]
    if not encrypted_content:
        raise CorruptedCiphertextError("ENC1 envelope has no encrypted content")
    return _Envelope(
        provider_id=provider_id,
        encrypted_data_key=encrypted_key,
        key_nonce=key_nonce,
        data_nonce=data_nonce,
        encrypted_content=encrypted_content,
    )


def _validate_local_envelope(envelope: _Envelope) -> None:
    if envelope.provider_id != LOCAL_PROVIDER_ID:
        raise CorruptedCiphertextError(f"unsupported security provider id: {envelope.provider_id}")
    if len(envelope.key_nonce) != NONCE_SIZE:
        raise CorruptedCiphertextError("encrypted data key nonce has invalid length")
    if len(envelope.data_nonce) != NONCE_SIZE:
        raise CorruptedCiphertextError("content nonce has invalid length")
    if len(envelope.encrypted_data_key) < 16:
        raise CorruptedCiphertextError("encrypted data key is too short")
    if len(envelope.encrypted_content) < 16:
        raise CorruptedCiphertextError("encrypted content is too short")


def _aes_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    aesgcm_type = AESGCM
    if aesgcm_type is None:
        _ensure_crypto()
        raise BackendError("cryptography AES-GCM support is unavailable")
    try:
        return aesgcm_type(key).encrypt(nonce, plaintext, aad)
    except Exception as exc:
        raise BackendError("AES-GCM encryption failed") from exc


def _aes_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    aesgcm_type = AESGCM
    if aesgcm_type is None:
        _ensure_crypto()
        raise BackendError("cryptography AES-GCM support is unavailable")
    try:
        return aesgcm_type(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:
        if _is_invalid_tag(exc):
            raise AuthenticationFailedError("AES-GCM authentication failed") from exc
        raise BackendError("AES-GCM decryption failed") from exc


def _is_invalid_tag(exc: Exception) -> bool:
    return InvalidTag is not None and isinstance(exc, InvalidTag)


def _effective_aad(context: SecurityContext | None, aad: bytes) -> bytes:
    context_bytes = json.dumps(
        _context_payload(context),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return b"AMSEC-AAD1" + len(context_bytes).to_bytes(4, "big") + context_bytes + aad


def _context_payload(context: SecurityContext | None) -> dict[str, Any]:
    scope = context.scope if context is not None else None
    metadata = context.metadata if context is not None else {}
    return {
        "scope": {
            "org": getattr(scope, "org", ""),
            "space": str(getattr(scope, "space", "")),
            "user": getattr(scope, "user", ""),
            "agent": getattr(scope, "agent", ""),
            "session": getattr(scope, "session", ""),
        },
        "purpose": context.purpose if context is not None else "",
        "metadata": {str(key): str(value) for key, value in sorted(metadata.items())},
    }


def _org_id(context: SecurityContext | None) -> str:
    if context is None:
        return ""
    return context.scope.org


def _key_aad(org_id: str) -> bytes:
    return b"agent-memory:security:data-key:v1:" + org_id.encode("utf-8")


def _decode_key_string(value: str, *, source: str) -> bytes:
    raw = value.strip()
    if raw.startswith("hex:"):
        return _decode_hex_key(raw[4:], source=source)
    if raw.startswith("base64:"):
        return _decode_b64_key(raw[7:], source=source)
    return _decode_hex_key(raw, source=source)


def _decode_hex_key(value: str, *, source: str) -> bytes:
    try:
        key = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise ValidationError(f"invalid hex encryption root key from {source}") from exc
    return _validate_root_key(key, source=source)


def _decode_b64_key(value: str, *, source: str) -> bytes:
    try:
        key = binascii.a2b_base64(value.strip(), strict_mode=True)
    except binascii.Error as exc:
        raise ValidationError(f"invalid base64 encryption root key from {source}") from exc
    return _validate_root_key(key, source=source)


def _validate_root_key(key: bytes, *, source: str) -> bytes:
    if len(key) != DATA_KEY_SIZE:
        raise ValidationError(
            f"encryption root key from {source} must be {DATA_KEY_SIZE} bytes"
        )
    return key


def _restrict_file_mode(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        raise BackendError(f"failed to set key file permissions: {path}") from exc



@SecurityProducer.register("local")
def _build(config):
    return LocalEnvelopeSecurityProvider(
        LocalKeyProvider(
            key_file=Factory.cfg_get(config, "key_file", _DEFAULT_KEY_FILE),
            key_hex=Factory.cfg_get(config, "key_hex", ""),
            key_b64=Factory.cfg_get(config, "key_b64", ""),
            key_env=Factory.cfg_get(config, "key_env", _DEFAULT_KEY_ENV),
            create_key_file=as_bool(
                Factory.cfg_get(config, "create_key_file", False),
                default=False,
            ),
        ),
        allow_plaintext=as_bool(
            Factory.cfg_get(config, "allow_plaintext", True),
            default=True,
        ),
    )
