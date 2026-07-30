"""存储层基类：所有 ``*Store`` 共享的契约。

统一 CRUD 动词（增删改查），各存储接口保持同一命名：

============ ==========================================================
动词          含义
============ ==========================================================
``insert``   增：新建记录（id 已存在时抛 :class:`~common.errors.ConflictError`）
``delete``   删：按 id 删除（幂等）
``update``   改：修改已有记录（id 不存在时抛 :class:`~common.errors.NotFoundError`）
``get``      查：按 id 点查（点查单条不存在时抛 ``NotFoundError``；批量查缺失的 id 省略）
============ ==========================================================

检索型存储（fulltext / vector / graph / fusion）在四个动词之上再提供
``search`` 查询；kv 提供 MemoryUnit ``list`` 查询、``exists`` 存在性查询与
``scan`` 范围扫描；fs 提供 ``stat`` 元信息查询。后端不可用等非预期失败统一抛
:class:`~common.errors.BackendError`。

**scope 隔离是存储层的原生职责**（不靠调用方纪律）：``scope`` 是每个 Store
方法的**显式第一入参**（``scope: Scope``，架构 §7「scope 是独立轴、显式串参，
不随查询对象携带、也不混进过滤条件」），不放进记录/查询结构体、也不编进
``metadata`` / ``filters``（后者仅承载 scope 之外的额外谓词）。

- 检索型存储（fulltext / vector / graph / fusion）：写入按入参 ``scope`` 落库，
  ``search`` / 按 id 的 ``get`` / ``delete`` 物理约束在该 ``scope`` 内，绝不跨
  scope 返回或影响。记录 ``id`` 是 scope 内逻辑主键（``insert`` 冲突 /
  ``update`` 缺失按 ``(scope, id)`` 判定），后端可用 scope+id 生成物理主键。
- ``kv`` / ``fs`` 是通用键值/二进制原语：``scope`` 入参用于**对 key / 路径做命名
  空间隔离**——同一逻辑 key 在不同 scope 下是相互隔离的不同物理键，由存储层
  原生承担（不再由上层拼前缀）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum


class StoreType(str, Enum):
    KV = "kv"
    FULLTEXT = "fulltext"
    VECTOR = "vector"
    GRAPH = "graph"
    FUSION = "fusion"
    FS = "fs"


class BaseStore(ABC):
    """所有存储后端的自描述契约。"""

    @abstractmethod
    def store_type(self) -> StoreType:
        """返回本存储的类型。"""

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛 :class:`~common.errors.HealthCheckError`。"""
