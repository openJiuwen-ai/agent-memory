"""LocalFSStore — 基于本地文件系统的 :class:`~storage.fs.FSStore` 实现。

原模态资产/原始负载落在 ``root/<scope 四段>/`` 下，``ref`` 即相对该 scope 子目录
的逻辑路径（``insert`` 的 ``key`` 直接作为 ``ref`` 返回）；后续 ``get/stat/delete``
凭 ``(scope, ref)`` 还原物理路径，故同一逻辑 ``key`` 在不同 scope 下天然隔离。
"""

from __future__ import annotations

import mimetypes
import shutil
from pathlib import Path
from typing import BinaryIO

from common.errors import (
    ConflictError,
    HealthCheckError,
    NotFoundError,
    ValidationError,
)
from common.factory.factory import Factory
from common.type_def import Scope
from storage.fs import FsProducer

from .._support import scope_segments, wrap_backend
from ..base import StoreType
from ..fs import FSStore
from ..types import FileStat


class LocalFSStore(FSStore):
    def __init__(self, *, root: str, create_root: bool = True) -> None:
        self._root = Path(root).resolve()
        if create_root:
            self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, scope: Scope, ref: str) -> Path:
        """把 ``(scope, ref)`` 解析为 ``root`` 下的绝对路径，并阻断目录穿越。"""
        base = self._root.joinpath(*scope_segments(scope))
        target = (base / ref).resolve()
        if target != base and base.resolve() not in target.parents:
            raise ValidationError(f"ref escapes scope root: {ref!r}")
        return target

    def store_type(self) -> StoreType:
        return StoreType.FS

    def health(self) -> None:
        if not self._root.is_dir():
            raise HealthCheckError(f"storage root not a directory: {self._root}")

    def insert(self, scope: Scope, key: str, data: BinaryIO) -> str:
        path = self._path(scope, key)
        if path.exists():
            raise ConflictError(entity="file", key=key)
        with wrap_backend(f"fs insert {key!r}"):
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as fh:
                shutil.copyfileobj(data, fh)
        return key

    def update(self, scope: Scope, ref: str, data: BinaryIO) -> str:
        path = self._path(scope, ref)
        if not path.exists():
            raise NotFoundError(entity="file", key=ref)
        with wrap_backend(f"fs update {ref!r}"):
            with open(path, "wb") as fh:
                shutil.copyfileobj(data, fh)
        return ref

    def delete(self, scope: Scope, ref: str) -> None:
        path = self._path(scope, ref)
        with wrap_backend(f"fs delete {ref!r}"):
            path.unlink(missing_ok=True)  # 幂等

    def get(self, scope: Scope, ref: str) -> BinaryIO:
        path = self._path(scope, ref)
        try:
            return open(path, "rb")
        except FileNotFoundError:
            raise NotFoundError(entity="file", key=ref) from None

    def stat(self, scope: Scope, ref: str) -> FileStat:
        path = self._path(scope, ref)
        try:
            st = path.stat()
        except FileNotFoundError:
            raise NotFoundError(entity="file", key=ref) from None
        content_type, _ = mimetypes.guess_type(str(path))
        return FileStat(
            ref=ref,
            size=st.st_size,
            content_type=content_type or "",
            created_at=st.st_ctime,
            updated_at=st.st_mtime,
        )


# -- 注册到 FsProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@FsProducer.register("local")
def _build(config):
    # root 在构造器中无默认值 → 必填，build 阶段校验；create_root 有默认值，可覆盖。
    return LocalFSStore(
        root=Factory.require_param(config, "root", backend="local FS"),
        create_root=Factory.cfg_get(config, "create_root", True),
    )
