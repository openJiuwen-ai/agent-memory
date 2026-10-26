# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""FS 内容加密：稳定对象标识绑定 AAD，读写均有界，不接受明文回退。"""

from __future__ import annotations

import io
import json
from typing import BinaryIO

from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.security.cryptography import CryptographyProducer, CryptographyProvider
from jiuwen_memory.common.security.types import CryptoContext
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.fs import FsProducer, FSStore
from jiuwen_memory.storage.security import (
    EnabledStoreSecurity,
    StoreEncryptionBinding,
    StoreSecurity,
)
from jiuwen_memory.storage.types import FileStat

DEFAULT_MAX_PLAINTEXT_BYTES = 64 * 1024 * 1024
_CIPHERTEXT_ALLOWANCE = 4096
_PURPOSE = "fs_object"


def _size(value: object, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValidationError(f"{name} must be an integer >= {minimum}")
    try:
        parsed = int(value)
    except ValueError:
        raise ValidationError(f"{name} must be an integer >= {minimum}") from None
    if parsed < minimum:
        raise ValidationError(f"{name} must be an integer >= {minimum}")
    return parsed


def _read_bounded(stream: BinaryIO, limit: int) -> bytes:
    buffer = bytearray()
    while len(buffer) <= limit:
        chunk = stream.read(min(1024 * 1024, limit + 1 - len(buffer)))
        if chunk == b"":
            break
        if not isinstance(chunk, bytes):
            raise ValidationError("encrypted FS requires a blocking binary stream")
        buffer.extend(chunk)
    if len(buffer) > limit:
        raise ValidationError(f"encrypted FS object exceeds {limit} bytes")
    return bytes(buffer)


def _context(scope: Scope, object_id: str) -> CryptoContext:
    return CryptoContext(scope=scope, purpose=_PURPOSE, object_id=object_id, format_version=1)


def _aad(scope: Scope, object_id: str) -> bytes:
    payload = {
        "version": 1,
        "purpose": _PURPOSE,
        "object_id": object_id,
        "scope": {key: getattr(scope, key) for key in ("org", "space", "user", "agent", "session")},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class EncryptedFSStore(FSStore):
    """文件内容整体加密；stat.size 为密文大小，调用方负责关闭返回的明文流。"""

    def __init__(
        self,
        inner: FSStore,
        encryption: CryptographyProvider,
        *,
        max_plaintext_bytes: int = DEFAULT_MAX_PLAINTEXT_BYTES,
        max_ciphertext_bytes: int = 0,
    ) -> None:
        if type(inner).object_id is FSStore.object_id:
            raise ValidationError("encrypted FS requires an inner backend with stable object IDs")
        self._inner = inner
        self._encryption = encryption
        self._max_plaintext_bytes = _size(max_plaintext_bytes, "max_plaintext_bytes", minimum=1)
        bound = _size(max_ciphertext_bytes, "max_ciphertext_bytes", minimum=0)
        self._max_ciphertext_bytes = bound or self._max_plaintext_bytes + _CIPHERTEXT_ALLOWANCE
        self._security = EnabledStoreSecurity(
            encryption.health, binding=StoreEncryptionBinding(inner, encryption, "ENC1/fs/aad-v1")
        )

    @property
    def security(self) -> StoreSecurity:
        return self._security

    def store_type(self) -> StoreType:
        return StoreType.FS

    def health(self) -> None:
        self._inner.health()
        self._encryption.health()

    def object_id(self, scope: Scope, key_or_ref: str, *, for_insert: bool = False) -> str:
        return self._inner.object_id(scope, key_or_ref, for_insert=for_insert)

    def _encrypt(self, scope: Scope, object_id: str, stream: BinaryIO) -> bytes:
        plaintext = _read_bounded(stream, self._max_plaintext_bytes)
        try:
            ciphertext = self._encryption.encrypt(
                plaintext, context=_context(scope, object_id), aad=_aad(scope, object_id)
            )
        except Exception as exc:
            raise BackendError("FS encryption failed") from exc
        if len(ciphertext) > self._max_ciphertext_bytes:
            raise ValidationError("encrypted FS ciphertext exceeds configured limit")
        return ciphertext

    def insert(self, scope: Scope, key: str, data: BinaryIO) -> str:
        object_id = self.object_id(scope, key, for_insert=True)
        ciphertext = self._encrypt(scope, object_id, data)
        ref = self._inner.insert(scope, key, io.BytesIO(ciphertext))
        if self.object_id(scope, ref) != object_id:
            raise BackendError("FS backend returned a reference to a different object")
        return ref

    def update(self, scope: Scope, ref: str, data: BinaryIO) -> str:
        object_id = self.object_id(scope, ref)
        ciphertext = self._encrypt(scope, object_id, data)
        result = self._inner.update(scope, ref, io.BytesIO(ciphertext))
        if self.object_id(scope, result) != object_id:
            raise BackendError("FS backend changed the encrypted object identifier")
        return result

    def get(self, scope: Scope, ref: str) -> BinaryIO:
        object_id = self.object_id(scope, ref)
        if self._inner.stat(scope, ref).size > self._max_ciphertext_bytes:
            raise ValidationError("encrypted FS ciphertext exceeds configured limit")
        with self._inner.get(scope, ref) as stream:
            ciphertext = _read_bounded(stream, self._max_ciphertext_bytes)
        try:
            plaintext = self._encryption.decrypt(
                ciphertext, context=_context(scope, object_id), aad=_aad(scope, object_id)
            )
        except Exception as exc:
            raise BackendError("FS decryption failed") from exc
        if len(plaintext) > self._max_plaintext_bytes:
            raise ValidationError("encrypted FS plaintext exceeds configured limit")
        return io.BytesIO(plaintext)

    def delete(self, scope: Scope, ref: str) -> None:
        self._inner.delete(scope, ref)

    def stat(self, scope: Scope, ref: str) -> FileStat:
        return self._inner.stat(scope, ref)


@FsProducer.register("encrypted")
def _build(config):
    inner = config.params.get("inner")
    if inner is None or isinstance(inner, str) and inner == config.name:
        raise ValidationError("encrypted FS requires an explicit inner store distinct from itself")
    return EncryptedFSStore(
        FsProducer.dep(config, "inner"),
        CryptographyProducer.dep(config),
        max_plaintext_bytes=config.get("max_plaintext_bytes", DEFAULT_MAX_PLAINTEXT_BYTES),
        max_ciphertext_bytes=config.get("max_ciphertext_bytes", 0),
    )
