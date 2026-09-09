# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ENC1 本地信封实现：LocalKeyProvider + LocalEnvelopeCryptographyProvider。

两个能力装在同一模块，因为它们共享同一套 ENC1 常量与 AES-GCM 原语；对外仍是两个
独立 Producer 注册（``key_provider.local`` 与 ``cryptography.local``），可各自被具名
引用与替换。

信封结构（F05 §信封格式）::

    root key --HKDF(purpose, org)--> org key --AES-GCM--> 包裹 per-value data key
                                                data key --AES-GCM--> 内容密文

    header: magic | version | algorithm id | 各段长度 | key id 长度 | key epoch
    body:   wrapped data key | key nonce | data nonce | key id | 内容密文+tag

**版本迁移**：写入一律用 v2（带 key id/epoch）；读取兼容 v1（无 key id/epoch，
用旧的派生与 AAD 布局）。v1 不再写出，也不会因为解不开而回退明文——不合法信封
一律拒绝（F05 §明文策略）。
"""

from __future__ import annotations

import binascii
import json
import os
import secrets
import struct
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.cryptography.base import (
    AuthenticationFailedError,
    CorruptedCiphertextError,
    CryptographyProducer,
    CryptographyProvider,
    InvalidMagicError,
    KeyMismatchError,
)
from jiuwen_memory.common.security.cryptography.key_provider import (
    KeyProvider,
    KeyProviderProducer,
    KeyRef,
    WrappedKey,
)
from jiuwen_memory.common.security.types import CryptoContext, reveal_secret

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
ENVELOPE_VERSION_V1 = 0x01  # 只读兼容：无 key id / epoch
ENVELOPE_VERSION = 0x02  # 当前写出版本
LOCAL_ALGORITHM_ID = 0x01  # AES-256-GCM 信封（原 provider_id，语义未变）
NONCE_SIZE = 12
DATA_KEY_SIZE = 32
_HEADER_V1 = struct.Struct("!4sBBHHH")
_HEADER = struct.Struct("!4sBBHHHBI")
_MAX_KEY_ID_LEN = 255  # key id 长度字段是 1 字节
_DEFAULT_KEY_FILE = "~/.agent-memory/security/master.key"
_DEFAULT_KEY_ENV = "AGENT_MEMORY_ENCRYPTION_ROOT_KEY"
_HKDF_SALT = b"agent-memory-security-local-salt-v1"
_KEY_ID_CHARS = 32  # 128 bit 指纹的十六进制长度


@dataclass(frozen=True)
class _Envelope:
    version: int
    algorithm_id: int
    wrapped_data_key: bytes
    key_nonce: bytes
    data_nonce: bytes
    key_id: str
    key_epoch: int
    encrypted_content: bytes


def _private_file_opener(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


# ====================================================================== #
# KeyProvider
# ====================================================================== #


class LocalKeyProvider(KeyProvider):
    """本地根密钥的 KeyProvider：单机或开发部署。

    **多代轮换**：:meth:`rotate` 生成新随机根密钥并推进 epoch，旧 epoch 的根密钥
    保留在进程内字典供 :meth:`unwrap` 解开历史信封。新根密钥**不持久化**--进程重启
    后回到配置声明的初始密钥，轮换后写入的信封在重启后不可读。需要跨重启保留轮换
    状态的部署应换 KMS/Vault 实现，由其管理历史 epoch 的验证材料。
    """

    def __init__(
        self,
        *,
        key_file: str = _DEFAULT_KEY_FILE,
        key_hex: str = "",
        key_b64: str = "",
        key_env: str = _DEFAULT_KEY_ENV,
        # 默认 fail-closed（F04 §create_key_file 默认值）：缺密钥源且 key_file 不存在
        # 时装配期抛 BackendError，不静默生成无人管理的根密钥。开发/单机显式传
        # True 才启用自动生成——容器 HOME / 挂载卷漂移后悄悄换钥匙，
        # 会直到读历史密文才暴露不可恢复的 KeyMismatchError。
        create_key_file: bool = False,
        key_epoch: int = 1,
    ) -> None:
        _ensure_crypto()
        if key_epoch < 1:
            raise ValidationError("key_epoch must be >= 1")
        self._key_file = Path(key_file).expanduser() if key_file else None
        self._key_hex = key_hex.strip()
        self._key_b64 = key_b64.strip()
        self._key_env = key_env.strip()
        self._create_key_file_enabled = create_key_file
        self._key_epoch = key_epoch
        self._root_key: bytes | None = None
        self._key_id: str = ""
        # 历史 epoch 的根密钥：rotate 时把旧代根密钥保留于此，供 unwrap 解开历史信封。
        self._keys: dict[int, bytes] = {}
        # wrap/unwrap/rotate 互斥：wrap 的「取 KeyRef + 取根密钥」必须是对同一代的
        # 原子快照，否则与 rotate 交错会写出「信封标 epoch1、密钥却是 epoch2」的
        # 永久不可解密信封。RLock 因锁内调用自身的 _load_root_key 等读取方法。
        self._lock = threading.RLock()

    # -- KeyProvider 契约 -------------------------------------------------- #

    def active_key(self) -> KeyRef:
        # key_id 与 epoch 必须来自同一代的原子快照。锁外读会在「取到旧 key_id 后、
        # 读 epoch 前」被 rotate 插入，返回「旧 key_id + 新 epoch」这样一个从未存在
        # 过的 KeyRef——它进 AAD 后写出的信封永远解不开。RLock 可重入，wrap/rotate
        # 在持锁状态下调本方法是安全的。
        with self._lock:
            return KeyRef(key_id=self._active_key_id(), epoch=self._key_epoch)

    def wrap(self, data_key: bytes, *, purpose: str, org: str) -> WrappedKey:
        if len(data_key) != DATA_KEY_SIZE:
            raise ValidationError(f"local data key must be {DATA_KEY_SIZE} bytes")
        # KeyRef 与根密钥必须取自同一代（原子快照），加密可锁外做但快照不行--
        # 见 _lock 字段注释的交错场景。
        with self._lock:
            ref = self.active_key()
            root_key = self._load_root_key()
        wrapping_key = self._derive_wrapping_key(purpose=purpose, org=org, root_key=root_key)
        nonce = secrets.token_bytes(NONCE_SIZE)
        ciphertext = _aes_encrypt(
            wrapping_key, nonce, data_key, _key_aad(purpose=purpose, org=org, ref=ref)
        )
        return WrappedKey(ciphertext=ciphertext, nonce=nonce, ref=ref)

    def unwrap(self, wrapped: WrappedKey, *, purpose: str, org: str) -> bytes:
        with self._lock:
            root_key = self._root_key_for_epoch(wrapped.ref)
        if root_key is None:
            # 找不到该 epoch 的保留材料即拒，不拿活动密钥试解--试解成功会让 epoch
            # 绑定形同虚设，失败则退化成难以诊断的 tag 校验错误。
            raise KeyMismatchError(
                "data key was wrapped by a different key generation "
                f"(epoch {wrapped.ref.epoch}); no retained key material for it"
            )
        wrapping_key = self._derive_wrapping_key(purpose=purpose, org=org, root_key=root_key)
        return self._unwrap_with(
            wrapping_key,
            wrapped,
            _key_aad(purpose=purpose, org=org, ref=wrapped.ref),
        )

    def rotate(self) -> KeyRef:
        # 保留当前 epoch 的根密钥供历史信封解密，再生成新随机根密钥推进 epoch。
        # 新根密钥**不持久化**（见类 docstring）：进程重启回到配置声明的初始密钥，
        # 轮换后写入的信封在重启后不可读。需要跨重启保留轮换状态应换 KMS/Vault。
        with self._lock:
            self._keys[self._key_epoch] = self._load_root_key()
            self._root_key = secrets.token_bytes(DATA_KEY_SIZE)
            self._key_epoch += 1
            self._key_id = ""  # 活动密钥指纹需重算
            return self.active_key()

    def health(self) -> None:
        self._load_root_key()

    # -- v1 只读兼容 ------------------------------------------------------- #

    def unwrap_legacy_v1(self, wrapped: WrappedKey, *, org: str) -> bytes:
        """解开 v1 信封的数据密钥：无 key id/epoch，用旧派生与旧 AAD。

        v1 的包裹密钥不含 purpose——用途隔离是本次迁移新增的，旧数据无从追认。
        """
        wrapping_key = self._derive_org_key_v1(org)
        return self._unwrap_with(wrapping_key, wrapped, _key_aad_v1(org))

    # -- 内部 -------------------------------------------------------------- #

    def _unwrap_with(self, wrapping_key: bytes, wrapped: WrappedKey, aad: bytes) -> bytes:
        try:
            data_key = _aes_decrypt(wrapping_key, wrapped.nonce, wrapped.ciphertext, aad)
        except AuthenticationFailedError as exc:
            raise KeyMismatchError("wrapped data key cannot be decrypted") from exc
        if len(data_key) != DATA_KEY_SIZE:
            raise CorruptedCiphertextError("unwrapped data key has invalid length")
        return data_key

    def _active_key_id(self) -> str:
        """根密钥的不可逆指纹，作 key id 写进信封。

        用 HKDF 从根密钥派生而不是直接哈希：派生结果与包裹密钥出自不同 info 标签，
        泄露 key id 不会给暴力破解根密钥提供额外杠杆。
        """
        if not self._key_id:
            digest = self._hkdf(
                info=b"agent-memory:security:key-id:v1",
                length=DATA_KEY_SIZE,
            )
            self._key_id = digest.hex()[:_KEY_ID_CHARS]
        return self._key_id

    def _derive_wrapping_key(
        self, *, purpose: str, org: str, root_key: bytes | None = None
    ) -> bytes:
        """按 (purpose, org) 派生包裹密钥——用途隔离 + 租户隔离（F05 §密钥隔离）。

        长度前缀防歧义：``purpose="a" org="b:c"`` 与 ``purpose="a:b" org="c"``
        直接拼接会得到同一个 info，从而共用同一把包裹密钥。
        """
        purpose_bytes = purpose.encode("utf-8")
        org_bytes = org.encode("utf-8")
        info = (
            b"agent-memory:security:kek:v2:"
            + len(purpose_bytes).to_bytes(4, "big")
            + purpose_bytes
            + len(org_bytes).to_bytes(4, "big")
            + org_bytes
        )
        return self._hkdf(info=info, length=DATA_KEY_SIZE, root_key=root_key)

    def _derive_org_key_v1(self, org_id: str) -> bytes:
        """v1 的按 org 派生（无 purpose）。只用于读旧信封。"""
        return self._hkdf(
            info=b"agent-memory:security:kek:v1:" + org_id.encode("utf-8"),
            length=DATA_KEY_SIZE,
        )

    def _hkdf(self, *, info: bytes, length: int, root_key: bytes | None = None) -> bytes:
        hkdf_type = HKDF
        hashes_module = hashes
        if hkdf_type is None or hashes_module is None:
            _ensure_crypto()
            raise BackendError("cryptography HKDF support is unavailable")
        hkdf = hkdf_type(
            algorithm=hashes_module.SHA256(),
            length=length,
            salt=_HKDF_SALT,
            info=info,
        )
        return hkdf.derive(root_key if root_key is not None else self._load_root_key())

    def _load_root_key(self) -> bytes:
        if self._root_key is None:
            self._root_key = self._load_or_create_root_key()
        return self._root_key

    def _root_key_for_epoch(self, ref: KeyRef) -> bytes | None:
        """取某 epoch 的根密钥：活动 epoch 用 ``_load_root_key``，旧 epoch 用 ``_keys``。

        ``key_id`` 不在此单独校验--旧 epoch 的 AAD 含 ``ref``（key_id+epoch），
        拿错材料派生的 wrapping_key 会被 AES-GCM 的 AAD 校验拒绝，表现为
        :class:`KeyMismatchError`。
        """
        if ref.epoch == self._key_epoch:
            return self._load_root_key()
        return self._keys.get(ref.epoch)

    def _load_or_create_root_key(self) -> bytes:
        if self._key_hex:
            return _decode_hex_key(self._key_hex, source="key_hex")
        if self._key_b64:
            return _decode_b64_key(self._key_b64, source="key_b64")

        env_value = os.environ.get(self._key_env) if self._key_env else None
        if env_value:
            return _decode_key_string(env_value, source=f"env {self._key_env}")

        if self._key_file is None:
            raise BackendError("local security requires key_hex, key_b64, key_env, or key_file")
        if self._key_file.exists():
            _restrict_file_mode(self._key_file)
            return _decode_hex_key(
                self._key_file.read_text(encoding="ascii").strip(),
                source=str(self._key_file),
            )
        if not self._create_key_file_enabled:
            raise BackendError(f"local security key file does not exist: {self._key_file}")
        return self._create_key_file()

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


# ====================================================================== #
# CryptographyProvider
# ====================================================================== #


class LocalEnvelopeCryptographyProvider(CryptographyProvider):
    """ENC1 AES-256-GCM 信封，密钥经 KeyProvider 取得。

    **无明文回退**（F05 §明文策略）：入参不是合法信封即拒绝读取，解密失败不返回
    原始 bytes。是否允许未加密存储由上层选用不同的存储适配器表达，不在本类里开关。
    """

    def __init__(self, key_provider: KeyProvider) -> None:
        _ensure_crypto()
        self._key_provider = key_provider

    def encrypt(self, plaintext: bytes, *, context: CryptoContext, aad: bytes = b"") -> bytes:
        data_key = secrets.token_bytes(DATA_KEY_SIZE)
        data_nonce = secrets.token_bytes(NONCE_SIZE)
        wrapped = self._key_provider.wrap(data_key, purpose=context.purpose, org=context.scope.org)
        key_id_bytes = wrapped.ref.key_id.encode("utf-8")
        if len(key_id_bytes) > _MAX_KEY_ID_LEN:
            raise ValidationError(
                f"key id is too long for the ENC1 envelope ({len(key_id_bytes)} bytes)"
            )
        encrypted_content = _aes_encrypt(
            data_key,
            data_nonce,
            plaintext,
            _content_aad(context, aad, ref=wrapped.ref),
        )
        return _build_envelope(
            wrapped=wrapped,
            key_id_bytes=key_id_bytes,
            data_nonce=data_nonce,
            encrypted_content=encrypted_content,
        )

    def decrypt(self, ciphertext: bytes, *, context: CryptoContext, aad: bytes = b"") -> bytes:
        if not ciphertext.startswith(ENVELOPE_MAGIC):
            raise InvalidMagicError("ciphertext is not an ENC1 envelope")

        envelope = _parse_envelope(ciphertext)
        _validate_local_envelope(envelope)
        wrapped = WrappedKey(
            ciphertext=envelope.wrapped_data_key,
            nonce=envelope.key_nonce,
            ref=KeyRef(key_id=envelope.key_id, epoch=envelope.key_epoch),
        )

        if envelope.version == ENVELOPE_VERSION_V1:
            data_key = self._unwrap_v1(wrapped, org=context.scope.org)
            content_aad = _content_aad_v1(context, aad)
        else:
            data_key = self._key_provider.unwrap(
                wrapped, purpose=context.purpose, org=context.scope.org
            )
            content_aad = _content_aad(context, aad, ref=wrapped.ref)

        return _aes_decrypt(data_key, envelope.data_nonce, envelope.encrypted_content, content_aad)

    def health(self) -> None:
        self._key_provider.health()

    def _unwrap_v1(self, wrapped: WrappedKey, *, org: str) -> bytes:
        """v1 信封的数据密钥解包，只有声明支持的 KeyProvider 能做。"""
        legacy = getattr(self._key_provider, "unwrap_legacy_v1", None)
        if legacy is None:
            raise CorruptedCiphertextError(
                "ENC1 v1 envelope requires a key provider with v1 read compatibility"
            )
        return legacy(wrapped, org=org)


# ====================================================================== #
# 信封编解码
# ====================================================================== #


def _ensure_crypto() -> None:
    if _CRYPTO_IMPORT_ERROR is not None:
        raise BackendError(
            "security.local requires the 'cryptography' package; install project dependencies"
        ) from _CRYPTO_IMPORT_ERROR


def _build_envelope(
    *,
    wrapped: WrappedKey,
    key_id_bytes: bytes,
    data_nonce: bytes,
    encrypted_content: bytes,
) -> bytes:
    header = _HEADER.pack(
        ENVELOPE_MAGIC,
        ENVELOPE_VERSION,
        LOCAL_ALGORITHM_ID,
        len(wrapped.ciphertext),
        len(wrapped.nonce),
        len(data_nonce),
        len(key_id_bytes),
        wrapped.ref.epoch,
    )
    return (
        header + wrapped.ciphertext + wrapped.nonce + data_nonce + key_id_bytes + encrypted_content
    )


def _parse_envelope(ciphertext: bytes) -> _Envelope:
    # 版本字节的位置在 v1/v2 一致（紧跟 magic），先读版本再选布局。
    if len(ciphertext) < _HEADER_V1.size:
        raise CorruptedCiphertextError("ENC1 envelope too short")
    version = ciphertext[len(ENVELOPE_MAGIC)]
    if version == ENVELOPE_VERSION_V1:
        return _parse_envelope_v1(ciphertext)
    if version != ENVELOPE_VERSION:
        raise CorruptedCiphertextError(f"unsupported ENC1 version: {version}")

    if len(ciphertext) < _HEADER.size:
        raise CorruptedCiphertextError("ENC1 envelope too short")
    (
        magic,
        _version,
        algorithm_id,
        key_len,
        key_nonce_len,
        data_nonce_len,
        key_id_len,
        key_epoch,
    ) = _HEADER.unpack(ciphertext[: _HEADER.size])
    if magic != ENVELOPE_MAGIC:
        raise InvalidMagicError("ciphertext is not an ENC1 envelope")

    offset = _HEADER.size
    body_len = key_len + key_nonce_len + data_nonce_len + key_id_len
    if len(ciphertext) < offset + body_len:
        raise CorruptedCiphertextError("ENC1 envelope length is incomplete")

    wrapped_data_key, offset = _take(ciphertext, offset, key_len)
    key_nonce, offset = _take(ciphertext, offset, key_nonce_len)
    data_nonce, offset = _take(ciphertext, offset, data_nonce_len)
    key_id_raw, offset = _take(ciphertext, offset, key_id_len)
    encrypted_content = ciphertext[offset:]
    if not encrypted_content:
        raise CorruptedCiphertextError("ENC1 envelope has no encrypted content")
    try:
        key_id = key_id_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorruptedCiphertextError("ENC1 envelope key id is not valid UTF-8") from exc

    return _Envelope(
        version=ENVELOPE_VERSION,
        algorithm_id=algorithm_id,
        wrapped_data_key=wrapped_data_key,
        key_nonce=key_nonce,
        data_nonce=data_nonce,
        key_id=key_id,
        key_epoch=key_epoch,
        encrypted_content=encrypted_content,
    )


def _parse_envelope_v1(ciphertext: bytes) -> _Envelope:
    magic, _version, algorithm_id, key_len, key_nonce_len, data_nonce_len = _HEADER_V1.unpack(
        ciphertext[: _HEADER_V1.size]
    )
    if magic != ENVELOPE_MAGIC:
        raise InvalidMagicError("ciphertext is not an ENC1 envelope")

    offset = _HEADER_V1.size
    body_len = key_len + key_nonce_len + data_nonce_len
    if len(ciphertext) < offset + body_len:
        raise CorruptedCiphertextError("ENC1 envelope length is incomplete")

    wrapped_data_key, offset = _take(ciphertext, offset, key_len)
    key_nonce, offset = _take(ciphertext, offset, key_nonce_len)
    data_nonce, offset = _take(ciphertext, offset, data_nonce_len)
    encrypted_content = ciphertext[offset:]
    if not encrypted_content:
        raise CorruptedCiphertextError("ENC1 envelope has no encrypted content")

    return _Envelope(
        version=ENVELOPE_VERSION_V1,
        algorithm_id=algorithm_id,
        wrapped_data_key=wrapped_data_key,
        key_nonce=key_nonce,
        data_nonce=data_nonce,
        key_id="",  # v1 无 key id
        key_epoch=0,  # 0 = 未声明，与合法 epoch（>=1）不会混淆
        encrypted_content=encrypted_content,
    )


def _take(buffer: bytes, offset: int, length: int) -> tuple[bytes, int]:
    end = offset + length
    return buffer[offset:end], end


def _validate_local_envelope(envelope: _Envelope) -> None:
    if envelope.algorithm_id != LOCAL_ALGORITHM_ID:
        raise CorruptedCiphertextError(f"unsupported algorithm id: {envelope.algorithm_id}")
    if len(envelope.key_nonce) != NONCE_SIZE:
        raise CorruptedCiphertextError("wrapped data key nonce has invalid length")
    if len(envelope.data_nonce) != NONCE_SIZE:
        raise CorruptedCiphertextError("content nonce has invalid length")
    if len(envelope.wrapped_data_key) < 16:
        raise CorruptedCiphertextError("wrapped data key is too short")
    if len(envelope.encrypted_content) < 16:
        raise CorruptedCiphertextError("encrypted content is too short")
    if envelope.version == ENVELOPE_VERSION and not envelope.key_id:
        raise CorruptedCiphertextError("ENC1 v2 envelope is missing the key id")


# ====================================================================== #
# AES-GCM 原语
# ====================================================================== #


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


# ====================================================================== #
# AAD 构造
# ====================================================================== #


def _content_aad(context: CryptoContext, aad: bytes, *, ref: KeyRef) -> bytes:
    """v2 内容 AAD：绑定 Scope、用途、对象 id、格式版本与 key ref（F05 §信封格式）。

    key ref 一并进 AAD，使信封头里的 key id/epoch 不能被替换成另一代——只写进头部
    而不参与认证的字段是可篡改的。
    """
    payload = _context_payload_v1(context)
    payload["object_id"] = context.object_id
    payload["format_version"] = context.format_version
    payload["key_id"] = ref.key_id
    payload["key_epoch"] = ref.epoch
    return _pack_aad(b"AMSEC-AAD2", payload, aad)


def _content_aad_v1(context: CryptoContext, aad: bytes) -> bytes:
    """v1 内容 AAD：只绑定 Scope、用途与 metadata。只用于读旧信封。"""
    return _pack_aad(b"AMSEC-AAD1", _context_payload_v1(context), aad)


def _pack_aad(tag: bytes, payload: dict[str, Any], aad: bytes) -> bytes:
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return tag + len(payload_bytes).to_bytes(4, "big") + payload_bytes + aad


def _context_payload_v1(context: CryptoContext) -> dict[str, Any]:
    scope = context.scope
    return {
        "scope": {
            "org": scope.org,
            "space": str(scope.space),
            "user": scope.user,
            "agent": scope.agent,
            "session": scope.session,
        },
        "purpose": context.purpose,
        "metadata": {str(k): str(v) for k, v in sorted(context.metadata.items())},
    }


def _key_aad(*, purpose: str, org: str, ref: KeyRef) -> bytes:
    """v2 包裹层 AAD：绑定用途、租户与 key ref。"""
    payload = {"purpose": purpose, "org": org, "key_id": ref.key_id, "key_epoch": ref.epoch}
    return _pack_aad(b"agent-memory:security:data-key:v2:", payload, b"")


def _key_aad_v1(org_id: str) -> bytes:
    """v1 包裹层 AAD：只绑定租户。只用于读旧信封。"""
    return b"agent-memory:security:data-key:v1:" + org_id.encode("utf-8")


# ====================================================================== #
# 根密钥解码
# ====================================================================== #


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
        raise ValidationError(f"encryption root key from {source} must be {DATA_KEY_SIZE} bytes")
    return key


def _restrict_file_mode(path: Path) -> None:
    if os.name == "nt":
        # NTFS 不映射 POSIX mode 位，os.chmod 在 Windows 上是无效近似。用 DACL
        # 显式收权（禁继承 + 仅当前服务账号），对应 0600 的 Windows 语义
        # （F04：根密钥文件只有运行账号可读）。
        _restrict_windows_acl(path)
        return
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        raise BackendError(f"failed to set key file permissions: {path}") from exc


def _restrict_windows_acl(path: Path) -> None:
    # Windows 的 key-file 模式要能防住「文件被预置了 Everyone/Users 显式读权限」的
    # 场景（AUTH-ENC-04 的 0600 等价语义）。只跑一次
    # ``icacls /inheritance:r /grant`` 会留下那些显式 ACE，因此必须用替换型 DACL 或
    # 显式清理不允许的 ACE——见 reviewer 在 PR1-SEC-03 的验收要求。这里分两步：
    #
    #  1. ``/reset``：把 DACL 重设为「从父继承」，清掉所有显式 ACE（含预置的
    #     Everyone/Users）。这一步不含清继承。
    #  2. ``/inheritance:r /grant:r <identity>:F``：关闭继承（去掉父目录流下来的
    #     ``(I)`` 项，以及 OWNER RIGHTS 等系统默认），再只给当前服务账号一条
    #     完整授权。两步连用 ``/reset /inheritance:r`` 会被 icacls 以
    #     "Invalid parameter" 拒绝（两者都改变继承状态），所以必须分开。
    #
    # 收权后 DACL 只含当前账号一条 ``(F)`` 且无 ``(I)``，与
    # ``_assert_windows_private`` 的断言一致。
    windows_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    icacls = windows_root / "System32" / "icacls.exe"
    if not icacls.is_absolute() or not icacls.is_file():
        raise BackendError("Windows system icacls.exe is unavailable")

    result = subprocess.run(
        [str(icacls), str(path), "/reset"],
        capture_output=True,
        text=True,
        encoding="locale",
        check=False,
    )
    if result.returncode != 0:
        raise BackendError(f"failed to reset key file permissions: {path}")
    result = subprocess.run(
        [
            str(icacls),
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{_windows_current_account_name()}:F",
        ],
        capture_output=True,
        text=True,
        encoding="locale",
        check=False,
    )
    if result.returncode != 0:
        raise BackendError(f"failed to set key file permissions: {path}")


def _windows_current_account_name() -> str:
    # whoami 输出全小写（如 ``host\user``），而 icacls /grant:r 区分大小写，
    # 会以 "Invalid parameter" 拒绝。用 Win32 ``GetUserNameExW`` 取完全限定名的
    # ``NameSamCompatible`` 形式（如 ``HOSTNAME\USER``）——大小写与 SID 精确一致，
    # 也是 icacls 接受的形式。ctypes 只在 Windows 路径下延迟导入。
    import ctypes

    name_sam_compatible = 2
    buffer = ctypes.create_unicode_buffer(1024)
    size = ctypes.c_ulong(len(buffer))
    ok = ctypes.windll.secur32.GetUserNameExW(name_sam_compatible, buffer, ctypes.byref(size))
    if not ok:
        raise BackendError("failed to resolve current account to secure key file")
    return buffer.value


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


# ====================================================================== #
# 注册
# ====================================================================== #


@KeyProviderProducer.register("local")
def _build_key_provider(config):
    return LocalKeyProvider(
        key_file=Factory.cfg_get(config, "key_file", _DEFAULT_KEY_FILE),
        key_hex=reveal_secret(Factory.cfg_get(config, "key_hex", "")),
        key_b64=reveal_secret(Factory.cfg_get(config, "key_b64", "")),
        key_env=Factory.cfg_get(config, "key_env", _DEFAULT_KEY_ENV),
        create_key_file=_as_bool(
            Factory.cfg_get(config, "create_key_file", False),
            default=False,
        ),
        key_epoch=int(Factory.cfg_get(config, "key_epoch", 1)),
    )


@CryptographyProducer.register("local")
def _build(config):
    return LocalEnvelopeCryptographyProvider(
        KeyProviderProducer.dep(config, "key_provider", default="local")
    )
