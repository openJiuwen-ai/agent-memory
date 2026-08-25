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
    """执行 `purpose_for_key` 操作。

    Args:
        key: 参数 key（str）。

    Returns:
        返回 str。
    """
    if key.startswith(MEMORY_KEY_PREFIX):
        return _PURPOSE_MEMORY_UNIT
    if key.startswith(MESSAGES_KEY_PREFIX):
        return _PURPOSE_RAW_MESSAGE
    return _PURPOSE_KV_VALUE


def _scope_payload(scope: Scope) -> dict[str, str]:
    """执行 `scope_payload` 操作。

    Args:
        scope: 参数 scope（Scope）。

    Returns:
        返回 dict[str, str]。
    """
    return {
        "org": scope.org,
        "space": str(getattr(scope, "space", "")),
        "user": scope.user,
        "agent": scope.agent,
        "session": scope.session,
    }


def _aad(scope: Scope, key: str, purpose: str) -> bytes:
    """执行 `aad` 操作。

    Args:
        scope: 参数 scope（Scope）。
        key: 参数 key（str）。
        purpose: 参数 purpose（str）。

    Returns:
        返回 bytes。
    """
    payload = {
        "version": _AAD_VERSION,
        "scope": _scope_payload(scope),
        "key": key,
        "purpose": purpose,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _security_context(scope: Scope, key: str, purpose: str) -> SecurityContext:
    """执行 `security_context` 操作。

    Args:
        scope: 参数 scope（Scope）。
        key: 参数 key（str）。
        purpose: 参数 purpose（str）。

    Returns:
        返回 SecurityContext。
    """
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
        """初始化 EncryptedKVStore。

        Args:
            raw: 参数 raw（KVStore）。
            security: 参数 security（SecurityProvider）。
        """
        self._raw = raw
        self._security = security
        self._store_security = EnabledStoreSecurity(security.health)

    @property
    def security(self) -> StoreSecurity:
        """返回 security 属性。

        Returns:
            返回 StoreSecurity。
        """
        return self._store_security

    def store_type(self) -> StoreType:
        """返回当前存储类型。

        Returns:
            返回 StoreType。
        """
        return StoreType.KV

    def health(self) -> None:
        """执行健康检查。"""
        self._raw.health()
        self._security.health()

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        """插入一条或多条记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
            value: 参数 value（bytes）。
            ttl: 参数 ttl（float）。
        """
        self._raw.insert(scope, key, self._encrypt(scope, key, value), ttl=ttl)

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        """更新已有记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
            value: 参数 value（bytes）。
            ttl: 参数 ttl（float）。
        """
        self._raw.update(scope, key, self._encrypt(scope, key, value), ttl=ttl)

    def delete(self, scope: Scope, key: str) -> None:
        """删除指定的记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
        """
        self._raw.delete(scope, key)

    def get(self, scope: Scope, key: str) -> bytes:
        """读取指定的记录或资源。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。

        Returns:
            返回 bytes。
        """
        return self._decrypt(scope, key, self._raw.get(scope, key))

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        # 委托 raw KV 一次性批量取密文（raw 已保证全命中、缺失即抛 NotFoundError），
        # 再逐项解密——AAD 绑定 scope+key+purpose，各 key AAD 不同，不能批量统一
        # 解密（与 list 同款逐项解密）。
        """执行 `mget` 操作。

        Args:
            scope: 参数 scope（Scope）。
            keys: 参数 keys（list[str]）。

        Returns:
            返回 list[bytes]。
        """
        return [
            self._decrypt(scope, key, raw)
            for key, raw in zip(keys, self._raw.mget(scope, keys))
        ]

    def exists(self, scope: Scope, key: str) -> bool:
        """检查指定记录或资源是否存在。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。

        Returns:
            返回 bool。
        """
        return self._raw.exists(scope, key)

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        """扫描指定范围内的记录。

        Args:
            scope: 参数 scope（Scope）。
            prefix: 参数 prefix（str）。

        Returns:
            返回 list[tuple[str, bytes]]。
        """
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
        """列出符合条件的记录或资源。

        Args:
            scope: 参数 scope（Scope）。
            offset: 参数 offset（int）。
            limit: 参数 limit（int）。
            memory_types: 参数 memory_types（list[str] | None）。
            filters: 参数 filters（FilterExpr | None）。
            extensions: 参数 extensions（dict[str, str] | None）。

        Returns:
            返回 KVMemoryListResult。
        """
        return list_memory_entries(
            self.scan(scope, MEMORY_KEY_PREFIX),
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extensions,
        )

    def scopes(self) -> list[Scope]:
        """执行 `scopes` 操作。

        Returns:
            返回 list[Scope]。
        """
        return self._raw.scopes()

    def _encrypt(self, scope: Scope, key: str, plaintext: bytes) -> bytes:
        """执行 `encrypt` 操作。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
            plaintext: 参数 plaintext（bytes）。

        Returns:
            返回 bytes。

        Raises:
            BackendError: 执行失败时抛出。
        """
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
        """执行 `decrypt` 操作。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
            ciphertext: 参数 ciphertext（bytes）。

        Returns:
            返回 bytes。

        Raises:
            BackendError: 执行失败时抛出。
        """
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
    """执行 `raw_kv_store` 操作。

    Args:
        config: 参数 config（Any）。

    Returns:
        返回 KVStore。

    Raises:
        ValidationError: 执行失败时抛出。
    """
    raw = config.params.get("raw_kv_store")
    if raw is None:
        raise ValidationError("kv_store.encrypted params.raw_kv_store 必须配置")
    if isinstance(raw, str) and raw == config.name:
        raise ValidationError("kv_store.encrypted params.raw_kv_store 不能指向自身")
    return KvProducer.dep(config, param_name="raw_kv_store")


@KvProducer.register("encrypted")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return EncryptedKVStore(
        raw=_raw_kv_store(config),
        security=SecurityProducer.dep(config),
    )
