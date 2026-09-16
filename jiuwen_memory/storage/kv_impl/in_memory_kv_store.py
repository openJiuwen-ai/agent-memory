# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~storage.kv.KVStore` 的纯内存键值存储。

按 scope 原生隔离（scope 折成命名空间键），支持统一 CRUD + ``scan`` 范围枚举。
``ttl`` 以秒计、``0`` 永不过期；过期键在访问（get/exists/scan）时惰性清除。
无外部依赖。
"""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import nullcontext
from threading import RLock

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, FilterExpr, Scope
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.types import KVMemoryListResult

from .memory_list import list_memory_entries
from .schema_source_index import MemorySourceIndex, property_sources

_ScopeKey = tuple[str, str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    """把 scope 折成可哈希的命名空间键（隔离单位）。"""
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


class InMemoryKVStore(KVStore):
    """纯内存键值存储：``{scope: {key: (value, expires_at)}}``，按 scope 隔离。"""

    def __init__(self, *, schema_source_index_enabled: bool = False) -> None:
        self._data: dict[_ScopeKey, dict[str, tuple[bytes, float | None]]] = (
            defaultdict(dict)
        )
        self._source_index_enabled = schema_source_index_enabled
        self._source_indexes: dict[_ScopeKey, MemorySourceIndex] = {}
        self._invalid_indexes: set[_ScopeKey] = set()
        self._lock = RLock() if schema_source_index_enabled else nullcontext()

    def store_type(self) -> StoreType:
        return StoreType.KV

    def health(self) -> None:
        return None

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        with self._lock:
            sk = _skey(scope)
            if self._live(sk, key) is not None:
                raise ConflictError("kv", key)
            self._write(sk, key, value, ttl)

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        with self._lock:
            sk = _skey(scope)
            if self._live(sk, key) is None:
                raise NotFoundError("kv", key)
            self._write(sk, key, value, ttl)

    def _write(self, sk: _ScopeKey, key: str, value: bytes, ttl: float) -> None:
        if self._source_index_enabled:
            projected = property_sources(key, value)
            was_ready = sk not in self._invalid_indexes
            self._invalid_indexes.add(sk)
            self._source_indexes.setdefault(sk, MemorySourceIndex()).replace(key, projected)
            self._data[sk][key] = (value, time.time() + ttl if ttl else None)
            if was_ready:
                self._invalid_indexes.discard(sk)
        else:
            self._data[sk][key] = (value, time.time() + ttl if ttl else None)

    def delete(self, scope: Scope, key: str) -> None:
        with self._lock:
            sk = _skey(scope)
            self._remove_relations(sk, key)
            self._data[sk].pop(key, None)

    def _remove_relations(self, sk: _ScopeKey, key: str) -> None:
        index = self._source_indexes.get(sk)
        if index is not None:
            was_ready = sk not in self._invalid_indexes
            self._invalid_indexes.add(sk)
            index.replace(key, set())
            if was_ready:
                self._invalid_indexes.discard(sk)

    def get(self, scope: Scope, key: str) -> bytes:
        with self._lock:
            value = self._live(_skey(scope), key)
            if value is None:
                raise NotFoundError("kv", key)
            return value

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        # 按下标一一对应；不去重，重复 key 各下标独立返回。任一缺失即报
        # NotFoundError（与 get 一致），不在批量点读里静默省略。
        with self._lock:
            sk = _skey(scope)
            out: list[bytes] = []
            for key in keys:
                value = self._live(sk, key)
                if value is None:
                    raise NotFoundError("kv", key)
                out.append(value)
            return out

    def exists(self, scope: Scope, key: str) -> bool:
        with self._lock:
            return self._live(_skey(scope), key) is not None

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        with self._lock:
            sk = _skey(scope)
            out: list[tuple[str, bytes]] = []
            for key in list(self._data[sk].keys()):  # list(...) 固化键，便于惰性删除
                value = self._live(sk, key)
                if value is not None and key.startswith(prefix):
                    out.append((key, value))
            return out

    def get_schema_properties_by_source(
        self, scope: Scope, source_id: str
    ) -> list[tuple[str, bytes]] | None:
        if not self._source_index_enabled:
            return None
        with self._lock:
            sk = _skey(scope)
            if sk in self._invalid_indexes:
                return None
            index = self._source_indexes.get(sk)
            if index is None:
                return []
            entries = []
            for key in list(index.by_source.get(source_id, ())):
                value = self._live(sk, key)
                if value is not None:
                    entries.append((key, value))
            return entries

    def rebuild_schema_source_index(self, scope: Scope) -> None:
        if not self._source_index_enabled:
            return super().rebuild_schema_source_index(scope)
        with self._lock:
            sk = _skey(scope)
            self._invalid_indexes.add(sk)
            index = MemorySourceIndex()
            for key, raw in self.scan(scope, MEMORY_KEY_PREFIX):
                index.replace(key, property_sources(key, raw))
            self._source_indexes[sk] = index
            self._invalid_indexes.discard(sk)

    def clear_schema_source_index(self, scope: Scope) -> None:
        if self._source_index_enabled:
            with self._lock:
                sk = _skey(scope)
                self._invalid_indexes.add(sk)
                self._source_indexes.pop(sk, None)

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
        with self._lock:
            return [
                Scope(org=k[0], space=k[1], user=k[2], agent=k[3], session=k[4])
                for k in self._data
            ]

    def _live(self, sk: _ScopeKey, key: str) -> bytes | None:
        """返回未过期的值；已过期则惰性删除并返回 None。"""
        rec = self._data[sk].get(key)
        if rec is None:
            return None
        value, expires_at = rec
        if expires_at is not None and expires_at <= time.time():
            self._remove_relations(sk, key)
            del self._data[sk][key]
            return None
        return value


# -- 注册到 KvProducer（接口层定义的工厂；实现自注册，新增无需改 producer/build_kernel） -------- #


@KvProducer.register("memory")
def _build(config):
    return InMemoryKVStore(schema_source_index_enabled=as_bool(
        Factory.cfg_get(config, "schema_source_index_enabled"), default=False
    ))
