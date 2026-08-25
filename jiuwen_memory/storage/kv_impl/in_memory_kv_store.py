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
        """初始化 InMemoryKVStore。"""
        self._data: dict[_ScopeKey, dict[str, tuple[bytes, float | None]]] = (
            defaultdict(dict)
        )

    def store_type(self) -> StoreType:
        """返回当前存储类型。

        Returns:
            返回 StoreType。
        """
        return StoreType.KV

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        """插入一条或多条记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
            value: 参数 value（bytes）。
            ttl: 参数 ttl（float）。

        Raises:
            ConflictError: 执行失败时抛出。
        """
        sk = _skey(scope)
        if self._live(sk, key) is not None:
            raise ConflictError("kv", key)
        self._data[sk][key] = (value, time.time() + ttl if ttl else None)

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        """更新已有记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
            value: 参数 value（bytes）。
            ttl: 参数 ttl（float）。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        sk = _skey(scope)
        if self._live(sk, key) is None:
            raise NotFoundError("kv", key)
        self._data[sk][key] = (value, time.time() + ttl if ttl else None)

    def delete(self, scope: Scope, key: str) -> None:
        """删除指定的记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
        """
        self._data[_skey(scope)].pop(key, None)

    def get(self, scope: Scope, key: str) -> bytes:
        """读取指定的记录或资源。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。

        Returns:
            返回 bytes。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        value = self._live(_skey(scope), key)
        if value is None:
            raise NotFoundError("kv", key)
        return value

    def mget(self, scope: Scope, keys: List[str]) -> List[bytes]:
        # 按下标一一对应；不去重，重复 key 各下标独立返回。任一缺失即报
        # NotFoundError（与 get 一致），不在批量点读里静默省略。
        """执行 `mget` 操作。

        Args:
            scope: 参数 scope（Scope）。
            keys: 参数 keys（List[str]）。

        Returns:
            返回 List[bytes]。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        sk = _skey(scope)
        out: List[bytes] = []
        for key in keys:
            value = self._live(sk, key)
            if value is None:
                raise NotFoundError("kv", key)
            out.append(value)
        return out

    def exists(self, scope: Scope, key: str) -> bool:
        """检查指定记录或资源是否存在。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。

        Returns:
            返回 bool。
        """
        return self._live(_skey(scope), key) is not None

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        """扫描指定范围内的记录。

        Args:
            scope: 参数 scope（Scope）。
            prefix: 参数 prefix（str）。

        Returns:
            返回 list[tuple[str, bytes]]。
        """
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
            del self._data[sk][key]
            return None
        return value


# -- 注册到 KvProducer（接口层定义的工厂；实现自注册，新增无需改 producer/build_kernel） -------- #


@KvProducer.register("memory")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return InMemoryKVStore()
