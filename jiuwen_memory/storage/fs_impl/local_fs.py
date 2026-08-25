"""LocalFSStore — 基于本地文件系统的 :class:`~storage.fs.FSStore` 实现。

原模态资产/原始负载落在 ``root/<scope 五段>/`` 下，``ref`` 即相对该 scope 子目录
的逻辑路径（``insert`` 的 ``key`` 直接作为 ``ref`` 返回）；后续 ``get/stat/delete``
凭 ``(scope, ref)`` 还原物理路径，故同一逻辑 ``key`` 在不同 scope 下天然隔离。
"""

from __future__ import annotations

import mimetypes
import shutil
from pathlib import Path
from typing import BinaryIO

from jiuwen_memory.common.errors import (
    ConflictError,
    HealthCheckError,
    NotFoundError,
    ValidationError,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.fs import FsProducer

from .._support import scope_segments, wrap_backend
from ..base import StoreType
from ..fs import FSStore
from ..types import FileStat


class LocalFSStore(FSStore):
    """本地文件系统存储；``root`` 可经 ConfigSource ``fs_store.root`` 晚绑定。"""

    def __init__(
        self,
        *,
        root: str,
        create_root: bool = True,
        config_source=None,
        config_namespace: str = "fs_store",
    ) -> None:
        """初始化 LocalFSStore。

        Args:
            root: 参数 root（str）。
            create_root: 参数 create_root（bool）。
            config_source: 参数 config_source。
            config_namespace: 参数 config_namespace（str）。
        """
        self._fallback_root = root
        self._create_root = create_root
        self._config_source = config_source
        self._config_namespace = config_namespace
        # 构造期确保回落 root 存在，便于开箱可用
        path = Path(root).resolve()
        if create_root:
            path.mkdir(parents=True, exist_ok=True)

    def store_type(self) -> StoreType:
        """返回当前存储类型。

        Returns:
            返回 StoreType。
        """
        return StoreType.FS

    def health(self) -> None:
        """执行健康检查。

        Raises:
            HealthCheckError: 执行失败时抛出。
        """
        root = self._resolved_root()
        if not root.is_dir():
            raise HealthCheckError(f"storage root not a directory: {root}")

    def insert(self, scope: Scope, key: str, data: BinaryIO) -> str:
        """插入一条或多条记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
            data: 参数 data（BinaryIO）。

        Returns:
            返回 str。

        Raises:
            ConflictError: 执行失败时抛出。
        """
        path = self._path(scope, key)
        if path.exists():
            raise ConflictError(entity="file", key=key)
        with wrap_backend(f"fs insert {key!r}"):
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as fh:
                shutil.copyfileobj(data, fh)
        return key

    def update(self, scope: Scope, ref: str, data: BinaryIO) -> str:
        """更新已有记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            ref: 参数 ref（str）。
            data: 参数 data（BinaryIO）。

        Returns:
            返回 str。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        path = self._path(scope, ref)
        if not path.exists():
            raise NotFoundError(entity="file", key=ref)
        with wrap_backend(f"fs update {ref!r}"):
            with open(path, "wb") as fh:
                shutil.copyfileobj(data, fh)
        return ref

    def delete(self, scope: Scope, ref: str) -> None:
        """删除指定的记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            ref: 参数 ref（str）。
        """
        path = self._path(scope, ref)
        with wrap_backend(f"fs delete {ref!r}"):
            path.unlink(missing_ok=True)  # 幂等

    def get(self, scope: Scope, ref: str) -> BinaryIO:
        """读取指定的记录或资源。

        Args:
            scope: 参数 scope（Scope）。
            ref: 参数 ref（str）。

        Returns:
            返回 BinaryIO。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        path = self._path(scope, ref)
        try:
            return open(path, "rb")
        except FileNotFoundError:
            raise NotFoundError(entity="file", key=ref) from None

    def stat(self, scope: Scope, ref: str) -> FileStat:
        """返回指定资源的元信息。

        Args:
            scope: 参数 scope（Scope）。
            ref: 参数 ref（str）。

        Returns:
            返回 FileStat。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
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

    def _resolved_root(self) -> Path:
        """解析并返回目标配置或资源。

        Returns:
            返回 Path。
        """
        from jiuwen_memory.config.binding import resolve_connection_url

        live = resolve_connection_url(
            self._config_source,
            namespace=self._config_namespace,
            field="root",
            fallback=self._fallback_root,
        )
        root = Path(live or self._fallback_root).resolve()
        if self._create_root:
            root.mkdir(parents=True, exist_ok=True)
        return root

    def _path(self, scope: Scope, ref: str) -> Path:
        """把 ``(scope, ref)`` 解析为 ``root`` 下的绝对路径，并阻断目录穿越。"""
        base = self._resolved_root().joinpath(*scope_segments(scope))
        target = (base / ref).resolve()
        if target != base and base.resolve() not in target.parents:
            raise ValidationError(f"ref escapes scope root: {ref!r}")
        return target


# -- 注册到 FsProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@FsProducer.register("local")
def _build(config):
    # root 在构造器中无默认值 → 必填，build 阶段校验；create_root 有默认值，可覆盖。
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    return LocalFSStore(
        root=Factory.require_param(config, "root", backend="local FS"),
        create_root=Factory.cfg_get(config, "create_root", True),
        config_source=ConfigSourceProducer.get_cached("default"),
    )
