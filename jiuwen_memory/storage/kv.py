# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""KVStore — 键值存储，统一 CRUD + 范围枚举。

``scope`` 为显式第一入参，对 key 做命名空间隔离（同一逻辑 ``key`` 在不同
scope 下互不可见）。``ttl`` 单位为秒（float），``0`` 表示永不过期。

在 CRUD 之上，``list`` 查询某 scope 下的 MemoryUnit，``scan`` 扫描原始
``(key, value)``，``scopes`` 列出有哪些 scope。通用扫描继续承载 lifecycle sweep /
Space offboarding，MemoryUnit 的过滤、计数和分页则由独立 list 契约承载。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import FilterExpr, Scope

from .base import BaseStore
from .types import KVMemoryListResult


class KvProducer(Factory):
    """KVStore 的注册式工厂（与契约同处一地，消费方只依赖接口层即可取实例）。

    ``name`` 即后端名（如 memory / sqlite / redis）。各实现在 ``kv_impl`` 下以
    ``@KvProducer.register("<后端>")`` 自注册——**注册发生在 import 实现模块时**，由
    :func:`storage.bootstrap.register_backends` 统一触发（装配入口在组装前调用一次）。
    """

    TOP_NAME = "kv_store"


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
    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        """批量读取 ``scope`` 下多个 ``key`` 的值。

        返回 ``list[bytes]`` 与 ``keys`` **按下标一一对应**；不去重，调用方可传重复
        key，各下标独立返回该 key 的值（语义同 Redis ``MGET``）。缺失语义与
        :meth:`get` 一致：任一 key 不存在即抛 :class:`~common.errors.NotFoundError`，
        不静默省略——批量点读不承担「索引↔真源短暂不一致」的兜底职责。省逐条 ``get``
        的接口往返是唯一目的；去重（如有需要）由调用方负责，不下沉到本接口。
        """

    @abstractmethod
    def exists(self, scope: Scope, key: str) -> bool:
        """返回 ``scope`` 下 ``key`` 是否存在。"""

    @abstractmethod
    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        """
        扫描 ``scope`` 下的全部 ``(key, value)``（可选只取 ``prefix`` 开头的
        key）。物理约束在该 ``scope`` 内，不跨 scope；已过期的键不返回。
        顺序由实现定义（调用方不应依赖）。
        """

    @abstractmethod
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
        """查询 ``scope`` 下的 MemoryUnit，返回当前页条目和分页前匹配总数。"""

    @abstractmethod
    def scopes(self) -> list[Scope]:
        """
        枚举本存储中已用过的全部 scope（命名空间）。与 :meth:`scan` 互补：
        ``scan`` 枚举一个 scope 内的键，``scopes`` 枚举有哪些 scope。供需要「无
        scope 入参做全局 sweep/offboarding」的管理任务使用。目标治理/生命周期操作
        必须携带显式 Scope，不得仅按 id 借此跨 scope 猜测。顺序由实现定义。
        """
