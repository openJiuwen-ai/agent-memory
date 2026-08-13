"""最小实现：:class:`~storage.kv.KVStore` 的纯内存键值存储。

按 scope 原生隔离（scope 折成命名空间键），支持统一 CRUD + ``scan`` 范围枚举。
``ttl`` 以秒计、``0`` 永不过期；过期键在访问（get/exists/scan）时惰性清除。
无外部依赖。
"""

from __future__ import annotations

import time
from collections import defaultdict

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, FilterExpr, Scope
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.types import KVMemoryListResult

from .memory_list import list_memory_entries

_ScopeKey = tuple[str, str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    """把 scope 折成可哈希的命名空间键（隔离单位）。"""
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


class InMemoryKVStore(KVStore):
    """纯内存键值存储：``{scope: {key: (value, expires_at)}}``，按 scope 隔离。"""

    def __init__(self) -> None:
        self._data: dict[_ScopeKey, dict[str, tuple[bytes, float | None]]] = (
            defaultdict(dict)
        )

    def store_type(self) -> StoreType:
        return StoreType.KV

    def health(self) -> None:
        return None

    def _live(self, sk: _ScopeKey, key: str) -> bytes | None:
        """返回未过期的值；已过期则惰性删除并返回 None。"""
        rec = self._data[sk].get(key)
        if rec is None:
            return None
        value, expires_at = rec
        if expires_at is not None and expires_at <= time.time():
            del self._data[sk][key]
            return None
        return value

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        sk = _skey(scope)
        if self._live(sk, key) is not None:
            raise ConflictError("kv", key)
        self._data[sk][key] = (value, time.time() + ttl if ttl else None)

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        sk = _skey(scope)
        if self._live(sk, key) is None:
            raise NotFoundError("kv", key)
        self._data[sk][key] = (value, time.time() + ttl if ttl else None)

    def delete(self, scope: Scope, key: str) -> None:
        self._data[_skey(scope)].pop(key, None)

    def get(self, scope: Scope, key: str) -> bytes:
        value = self._live(_skey(scope), key)
        if value is None:
            raise NotFoundError("kv", key)
        return value

    def mget(self, scope: Scope, keys: List[str]) -> List[bytes]:
        # 按下标一一对应；不去重，重复 key 各下标独立返回。任一缺失即报
        # NotFoundError（与 get 一致），不在批量点读里静默省略。
        sk = _skey(scope)
        out: List[bytes] = []
        for key in keys:
            value = self._live(sk, key)
            if value is None:
                raise NotFoundError("kv", key)
            out.append(value)
        return out

    def exists(self, scope: Scope, key: str) -> bool:
        return self._live(_skey(scope), key) is not None

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        sk = _skey(scope)
        out: list[tuple[str, bytes]] = []
        for key in list(self._data[sk].keys()):  # list(...) 固化键，便于惰性删除
            value = self._live(sk, key)
            if value is not None and key.startswith(prefix):
                out.append((key, value))
        return out

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
        return [
            Scope(org=k[0], space=k[1], user=k[2], agent=k[3], session=k[4])
            for k in self._data
        ]


# -- 注册到 KvProducer（接口层定义的工厂；实现自注册，新增无需改 producer/build_kernel） -------- #


@KvProducer.register("memory")
def _build(config):
    return InMemoryKVStore()
