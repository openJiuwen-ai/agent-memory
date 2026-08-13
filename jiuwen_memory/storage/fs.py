"""FSStore — 本地文件系统存储（原始负载/二进制资产）。

CRUD 映射到文件操作：``insert`` = 写入并返回规范引用 ref，
``update`` = 覆写，``delete`` = 删除，``get`` = 打开读取，
另提供 ``stat`` 元信息查询。``scope`` 为显式第一入参，对存储路径做命名空间
隔离（同一逻辑 ``key`` / ``ref`` 在不同 scope 下落在互不可见的物理路径）。
"""

from __future__ import annotations

from abc import abstractmethod
from typing import BinaryIO

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope

from .base import BaseStore
from .types import FileStat


class FsProducer(Factory):
    """FSStore 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即后端名（如 memory / local）。各实现在 ``fs_impl`` 下以
    ``@FsProducer.register("<后端>")`` 自注册——注册发生在 import 实现模块时，由
    :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "fs_store"


class FSStore(BaseStore):
    @abstractmethod
    def insert(self, scope: Scope, key: str, data: BinaryIO) -> str:
        """在 ``scope`` 下的 ``key`` 写入新文件，返回规范引用 ref；已存在时报冲突。"""

    @abstractmethod
    def update(self, scope: Scope, ref: str, data: BinaryIO) -> str:
        """覆写 ``scope`` 下 ``ref`` 处的文件，返回（可能更新的）ref；不存在时报缺失。"""

    @abstractmethod
    def delete(self, scope: Scope, ref: str) -> None:
        """删除 ``scope`` 下 ``ref`` 处的文件（幂等）。"""

    @abstractmethod
    def get(self, scope: Scope, ref: str) -> BinaryIO:
        """打开 ``scope`` 下 ``ref`` 处的文件用于读取，由调用方负责关闭。"""

    @abstractmethod
    def stat(self, scope: Scope, ref: str) -> FileStat:
        """返回 ``scope`` 下 ``ref`` 处文件的元信息。"""
