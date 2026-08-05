"""EncryptedKVStore — KVStore 加密装饰器。

该实现不包含具体加解密算法，只在 KV 边界统一构造
``CryptoContext`` / AAD，并委托注入的 ``CryptographyProvider``。真实算法位于
``common.security.cryptography.cryptography_impl``；本类只负责把所有 KV value 的
写前加密、读后解密收敛到同一个存储装饰器。
"""

from __future__ import annotations

import json
from typing import Any

from common.errors import BackendError, ValidationError
from common.security.cryptography import CryptographyProducer, CryptographyProvider
from common.security.types import CryptoContext
from common.type_def import MEMORY_KEY_PREFIX, MESSAGES_KEY_PREFIX, FilterExpr, Scope
from storage.base import StoreType
from storage.kv import KvProducer, KVStore
from storage.types import KVMemoryListResult

from .memory_list import list_memory_entries

_AAD_VERSION = 1
_PURPOSE_MEMORY_UNIT = "memory_unit"
_PURPOSE_RAW_MESSAGE = "raw_message"
_PURPOSE_KV_VALUE = "kv_value"


def _purpose_for_key(key: str) -> str:
    if key.startswith(MEMORY_KEY_PREFIX):
        return _PURPOSE_MEMORY_UNIT
    if key.startswith(MESSAGES_KEY_PREFIX):
        return _PURPOSE_RAW_MESSAGE
    return _PURPOSE_KV_VALUE


def _scope_payload(scope: Scope) -> dict[str, str]:
    return {
        "org": scope.org,
        "space": str(getattr(scope, "space", "")),
        "user": scope.user,
        "agent": scope.agent,
        "session": scope.session,
    }


def _aad(scope: Scope, key: str, purpose: str) -> bytes:
    payload = {
        "version": _AAD_VERSION,
        "scope": _scope_payload(scope),
        "key": key,
        "purpose": purpose,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _crypto_context(scope: Scope, key: str, purpose: str) -> CryptoContext:
    """对象标识与格式版本走 :class:`CryptoContext` 的专有字段，不再塞 metadata。

    它们是 F05 §信封格式要求 AAD 必须绑定的项，由类型显式承载才能保证每个调用点
    都带上——放 metadata 里漏掉一个不会有任何提示。
    """
    return CryptoContext(
        scope=scope,
        purpose=purpose,
        object_id=key,
        format_version=_AAD_VERSION,
    )


class EncryptedKVStore(KVStore):
    """对任意 KVStore 做透明加解密的装饰器。"""

    def __init__(self, raw: KVStore, encryption: CryptographyProvider) -> None:
        self._raw = raw
        self._encryption = encryption

    def store_type(self) -> StoreType:
        return StoreType.KV

    def health(self) -> None:
        self._raw.health()
        self._encryption.health()

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        self._raw.insert(scope, key, self._encrypt(scope, key, value), ttl=ttl)

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        self._raw.update(scope, key, self._encrypt(scope, key, value), ttl=ttl)

    def delete(self, scope: Scope, key: str) -> None:
        self._raw.delete(scope, key)

    def get(self, scope: Scope, key: str) -> bytes:
        return self._decrypt(scope, key, self._raw.get(scope, key))

    def exists(self, scope: Scope, key: str) -> bool:
        return self._raw.exists(scope, key)

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        return [
            (key, self._decrypt(scope, key, value))
            for key, value in self._raw.scan(scope, prefix=prefix)
        ]

    def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        filters: FilterExpr | None = None,
        extensions: dict[str, str] | None = None,
    ) -> KVMemoryListResult:
        return list_memory_entries(
            self.scan(scope, MEMORY_KEY_PREFIX),
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extensions,
        )

    def scopes(self) -> list[Scope]:
        return self._raw.scopes()

    def _encrypt(self, scope: Scope, key: str, plaintext: bytes) -> bytes:
        purpose = _purpose_for_key(key)
        context = _crypto_context(scope, key, purpose)
        aad = _aad(scope, key, purpose)
        try:
            return self._encryption.encrypt(plaintext, context=context, aad=aad)
        except Exception as exc:
            raise BackendError(f"kv encryption failed: key={key!r} purpose={purpose!r}") from exc

    def _decrypt(self, scope: Scope, key: str, ciphertext: bytes) -> bytes:
        purpose = _purpose_for_key(key)
        context = _crypto_context(scope, key, purpose)
        aad = _aad(scope, key, purpose)
        try:
            return self._encryption.decrypt(ciphertext, context=context, aad=aad)
        except Exception as exc:
            raise BackendError(f"kv decryption failed: key={key!r} purpose={purpose!r}") from exc


def _raw_kv_store(config: Any) -> KVStore:
    raw = config.params.get("raw_kv_store")
    if raw is None:
        raise ValidationError("kv_store.encrypted params.raw_kv_store 必须配置")
    if isinstance(raw, str) and raw == config.name:
        raise ValidationError("kv_store.encrypted params.raw_kv_store 不能指向自身")
    return KvProducer.dep(config, param_name="raw_kv_store")


@KvProducer.register("encrypted")
def _build(config):
    return EncryptedKVStore(
        raw=_raw_kv_store(config),
        encryption=CryptographyProducer.dep(config),
    )
