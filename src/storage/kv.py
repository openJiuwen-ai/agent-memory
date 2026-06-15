"""KVStore — 键值存储，统一 CRUD。

``scope`` 为显式第一入参，对 key 做命名空间隔离（同一逻辑 ``key`` 在不同
scope 下互不可见）。``ttl`` 单位为秒（float），``0`` 表示永不过期。
"""

from __future__ import annotations

from abc import abstractmethod

from common.type_def import Scope

from .base import BaseStore


class KVStore(BaseStore):
    @abstractmethod
    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        """在 ``scope`` 下新建 ``key``；已存在时报冲突。"""

    @abstractmethod
    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        """覆写 ``scope`` 下已有 ``key``；不存在时报缺失。"""

    @abstractmethod
    def delete(self, scope: Scope, key: str) -> None:
        """删除 ``scope`` 下的 ``key``（幂等）。"""

    @abstractmethod
    def get(self, scope: Scope, key: str) -> bytes:
        """读取 ``scope`` 下 ``key`` 的值；不存在时报缺失。"""

    @abstractmethod
    def exists(self, scope: Scope, key: str) -> bool:
        """返回 ``scope`` 下 ``key`` 是否存在。"""
