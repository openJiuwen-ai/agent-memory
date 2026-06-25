"""最小实现：:class:`~storage.kv.KVStore` 的纯内存键值存储。

按 scope 原生隔离（scope 折成命名空间键），支持统一 CRUD + ``list`` 范围枚举。
``ttl`` 以秒计、``0`` 永不过期；过期键在访问（get/exists/list）时惰性清除。
无外部依赖。
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from common.errors import ConflictError, NotFoundError
from common.type_def import Scope
from storage.base import StoreType
from storage.kv import KvProducer, KVStore

_ScopeKey = Tuple[str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    """把 scope 折成可哈希的命名空间键（隔离单位）。"""
    return (scope.org, scope.user, scope.agent, scope.session)


class InMemoryKVStore(KVStore):
    """纯内存键值存储：``{scope: {key: (value, expires_at)}}``，按 scope 隔离。"""

    def __init__(self) -> None:
        self._data: Dict[_ScopeKey, Dict[str, Tuple[bytes, Optional[float]]]] = (
            defaultdict(dict)
        )

    def store_type(self) -> StoreType:
        return StoreType.KV

    def health(self) -> None:
        return None

    def _live(self, sk: _ScopeKey, key: str) -> Optional[bytes]:
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

    def exists(self, scope: Scope, key: str) -> bool:
        return self._live(_skey(scope), key) is not None

    def list(self, scope: Scope, prefix: str = "") -> List[Tuple[str, bytes]]:
        sk = _skey(scope)
        out: List[Tuple[str, bytes]] = []
        for key in list(self._data[sk].keys()):  # list(...) 固化键，便于惰性删除
            value = self._live(sk, key)
            if value is not None and key.startswith(prefix):
                out.append((key, value))
        return out

    def scopes(self) -> List[Scope]:
        return [Scope(org=k[0], user=k[1], agent=k[2], session=k[3]) for k in self._data]


# -- 注册到 KvProducer（接口层定义的工厂；实现自注册，新增无需改 producer/build_kernel） -------- #


@KvProducer.register("memory")
def _build(config):
    return InMemoryKVStore()
