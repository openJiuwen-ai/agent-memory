# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~storage.fs.FSStore`——内存「文件系统」存原始二进制资产。

存 ``MemoryUnit.assets`` 指向的原模态原件（图片/音频/原始文档等）：``insert`` 读入
流、落库、返回规范引用 ref（``scope`` 做命名空间隔离）；``get`` 返回可读流；``stat``
给元信息。按 scope 隔离。无外部依赖（数据留在内存）。
"""

from __future__ import annotations

import io
import time
from collections import defaultdict
from typing import BinaryIO, Dict, Tuple

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.fs import FsProducer, FSStore
from jiuwen_memory.storage.types import FileStat

_ScopeKey = Tuple[str, str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def _ref(scope: Scope, key: str) -> str:
    """规范引用：scope 命名空间 + key（写入方据此回读/溯源）。"""
    return f"fs://{scope.org}/{scope.space}/{scope.user}/{scope.agent}/{scope.session}/{key}"


class _Blob:
    __slots__ = ("data", "created_at", "updated_at")

    def __init__(self, data: bytes, now: float) -> None:
        self.data = data
        self.created_at = now
        self.updated_at = now


class InMemoryFSStore(FSStore):
    """纯内存二进制存储：ref 寻址，按 scope 隔离。"""

    def __init__(self) -> None:
        self._data: Dict[_ScopeKey, Dict[str, _Blob]] = defaultdict(dict)

    def store_type(self) -> StoreType:
        return StoreType.FS

    def health(self) -> None:
        return None

    def insert(self, scope: Scope, key: str, data: BinaryIO) -> str:
        ref = _ref(scope, key)
        bucket = self._data[_skey(scope)]
        if ref in bucket:
            raise ConflictError("file", ref)
        bucket[ref] = _Blob(data.read(), time.time())
        return ref

    def update(self, scope: Scope, ref: str, data: BinaryIO) -> str:
        bucket = self._data[_skey(scope)]
        if ref not in bucket:
            raise NotFoundError("file", ref)
        blob = bucket[ref]
        blob.data = data.read()
        blob.updated_at = time.time()
        return ref

    def delete(self, scope: Scope, ref: str) -> None:
        self._data[_skey(scope)].pop(ref, None)

    def get(self, scope: Scope, ref: str) -> BinaryIO:
        bucket = self._data[_skey(scope)]
        if ref not in bucket:
            raise NotFoundError("file", ref)
        return io.BytesIO(bucket[ref].data)

    def stat(self, scope: Scope, ref: str) -> FileStat:
        bucket = self._data[_skey(scope)]
        if ref not in bucket:
            raise NotFoundError("file", ref)
        blob = bucket[ref]
        return FileStat(
            ref=ref,
            size=len(blob.data),
            content_type="application/octet-stream",
            created_at=blob.created_at,
            updated_at=blob.updated_at,
        )


# -- 注册到 FsProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@FsProducer.register("memory")
def _build(config):
    return InMemoryFSStore()
