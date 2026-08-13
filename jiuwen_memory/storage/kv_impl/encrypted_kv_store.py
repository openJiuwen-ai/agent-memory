"""EncryptedKVStore — KVStore 加密装饰器。

该实现不包含具体加解密算法，只在 KV 边界统一构造
``SecurityContext`` / AAD，并委托注入的 ``SecurityProvider``。真实算法位于
``common.security.security_impl``；本类只负责把所有 KV value 的写前加密、读后解密
收敛到同一个存储装饰器。
"""

from __future__ import annotations

import json
from typing import Any

from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.security import SecurityContext, SecurityProducer, SecurityProvider
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, MESSAGES_KEY_PREFIX, FilterExpr, Scope
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.security import EnabledStoreSecurity, StoreSecurity
from jiuwen_memory.storage.types import KVMemoryListResult

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


def _security_context(scope: Scope, key: str, purpose: str) -> SecurityContext:
    return SecurityContext(
        scope=scope,
        purpose=purpose,
        metadata={
            "key": key,
            "aad_version": str(_AAD_VERSION),
        },
    )


class EncryptedKVStore(KVStore):
    """对任意 KVStore 做透明加解密的装饰器。"""

    def __init__(self, raw: KVStore, security: SecurityProvider) -> None:
        self._raw = raw
        self._security = security
        self._store_security = EnabledStoreSecurity(security.health)

    @property
    def security(self) -> StoreSecurity:
        return self._store_security

    def store_type(self) -> StoreType:
        return StoreType.KV

    def health(self) -> None:
        self._raw.health()
        self._security.health()

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        self._raw.insert(scope, key, self._encrypt(scope, key, value), ttl=ttl)

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        self._raw.update(scope, key, self._encrypt(scope, key, value), ttl=ttl)

    def delete(self, scope: Scope, key: str) -> None:
        self._raw.delete(scope, key)

    def get(self, scope: Scope, key: str) -> bytes:
        return self._decrypt(scope, key, self._raw.get(scope, key))

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        # 委托 raw KV 一次性批量取密文（raw 已保证全命中、缺失即抛 NotFoundError），
        # 再逐项解密——AAD 绑定 scope+key+purpose，各 key AAD 不同，不能批量统一
        # 解密（与 list 同款逐项解密）。
        return [
            self._decrypt(scope, key, raw)
            for key, raw in zip(keys, self._raw.mget(scope, keys))
        ]

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
        context = _security_context(scope, key, purpose)
        aad = _aad(scope, key, purpose)
        try:
            return self._security.encrypt(plaintext, context=context, aad=aad)
        except Exception as exc:
            raise BackendError(
                f"kv encryption failed: key={key!r} purpose={purpose!r}"
            ) from exc

    def _decrypt(self, scope: Scope, key: str, ciphertext: bytes) -> bytes:
        purpose = _purpose_for_key(key)
        context = _security_context(scope, key, purpose)
        aad = _aad(scope, key, purpose)
        try:
            return self._security.decrypt(ciphertext, context=context, aad=aad)
        except Exception as exc:
            raise BackendError(
                f"kv decryption failed: key={key!r} purpose={purpose!r}"
            ) from exc


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
        security=SecurityProducer.dep(config),
    )
