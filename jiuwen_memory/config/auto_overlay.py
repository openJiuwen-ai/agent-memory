# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""从可信的启动位置定位本地配置覆盖库。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.config.binding import _canonical_local_path, resolve_connection_url
from jiuwen_memory.config.config import Config
from jiuwen_memory.config.config_source import ConfigSourceProducer
from jiuwen_memory.config.config_source_impl import register_config_sources
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.config.document_flag import should_write_document
from jiuwen_memory.config.overlay_store import SQLiteOverlayStore

logger = get_logger(__name__)


def _open_overlay(path: Path, instance_id: str = "default") -> SQLiteOverlayStore | None:
    """打开覆盖库；仅当覆盖文件尚不存在且无法创建时降级为只读配置。

    已经存在的覆盖文件读取失败必须继续 fail closed：静默忽略已保存的版本会让运行
    配置悄悄退回启动输入，且页面仍可能把保存结果当成已生效。首次创建失败通常来自
    只读目录，此时没有历史状态可丢，保留记忆服务并把配置留在只读即可。
    """
    try:
        existed = path.exists()
    except OSError:
        # 无法确认是否已有持久状态时按已存在处理，避免丢弃已保存的覆盖。
        existed = True
    try:
        return SQLiteOverlayStore(path, instance_id=instance_id)
    except (OSError, sqlite3.Error) as exc:
        if existed:
            raise BackendError("cannot access the instance configuration overlay") from exc
        logger.warning(
            "configuration overlay at %s cannot be created; configuration is read-only: %s",
            path,
            exc,
        )
        return None


def _startup_context(config):
    ctx = default_context()
    if config is not None:
        supplied = config if isinstance(config, Config) else Config.from_dict(config)
        ctx = ctx.merged(supplied.context())
    return ctx


def _local_document_context(config):
    ctx = _startup_context(config)
    if not as_bool(ctx.globals.get("write_document"), default=False):
        return None
    source = ctx.namespaces.get("config_source", {}).get("default")
    if source is None or source.target != "yaml_defaults":
        return None
    document = ctx.namespaces.get("markdown_store", {}).get("default")
    if document is not None and document.target == "local":
        return ctx
    return None


def _document_root(startup, fallback):
    return resolve_connection_url(
        startup, namespace="markdown_store", field="root", fallback=fallback,
    )


def _document_portal_paths(ctx) -> tuple[str, str] | None:
    """Resolve the Markdown root and index of the fixed, local SDK document store."""
    if ctx is None:
        return None
    document = ctx.namespaces["markdown_store"]["default"]
    shadow = ctx.namespaces.get("shadow_index", {}).get("default")
    if not shadow or shadow.target != "sqlite":
        return None
    engine = ctx.namespaces["engine"]["default"]
    # These engines read from their selected DomainStore. InMemoryEngine reads its KV port.
    if engine.target not in {"agent", "cloud"}:
        return None
    manager_name = engine.params.get("store_manager", ctx.globals.get("store_manager", "default"))
    manager = ctx.namespaces.get("store_manager", {}).get(manager_name)
    if not manager or manager.target != "composite":
        return None
    domains = manager.params.get("domain_stores") or {}
    if not isinstance(domains, Mapping):
        return None
    domain_name = engine.params.get("domain_store", "default")
    base_domain = domains.get("default", {})
    selected_domain = domains.get(domain_name, {})
    if not isinstance(base_domain, Mapping) or not isinstance(selected_domain, Mapping):
        return None
    domain = {**base_domain, **selected_domain}
    document_mode = should_write_document(
        domain.get("write_document", ctx.globals["write_document"]),
    )
    if domain.get("domain_store_target", "composite") != "composite" or not document_mode:
        return None

    register_config_sources()
    startup = ConfigSourceProducer.build_named("default", ctx)
    root = _document_root(startup, document.params.get("root", ctx.globals.get("root")))
    if not root or not Path(root).is_absolute():
        return None
    index = resolve_connection_url(
        startup, namespace="shadow_index", field="db_path",
        fallback=shadow.params.get("db_path", ctx.globals.get("db_path")),
    )
    if index is None:
        markdown_root = _document_root(startup, document.params.get("root"))
        if markdown_root:
            index = os.path.join(markdown_root, "memory", ".shadow", "shadow.db")
    if index is None or not Path(index).is_absolute():
        return None
    return str(Path(root).resolve()), str(Path(index).resolve())


def _prepare_shared_document_portal(
    config: Mapping[str, Any] | Config | None, *, instance_id: str,
) -> tuple[tuple[str, str], Callable[[], SQLiteOverlayStore | None]] | None:
    """Find a fixed document library and defer opening its configuration overlay."""
    ctx = _local_document_context(config)
    paths = _document_portal_paths(ctx)
    if paths is None:
        return None
    root, index = map(Path, paths)
    if sys.platform in {"darwin", "win32"}:
        try:
            if sys.platform == "win32" and not all(_local_ntfs(path) for path in (root, index)):
                return None
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            index.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Reserve a new SQLite filename before hashing it so both starters see one spelling.
            paths = _canonical_local_path(root), _canonical_local_path(index, create=True)
        except OSError as exc:
            logger.warning("shared Portal could not identify the local document library: %s", exc)
            return None
    encrypted = as_bool(ctx.globals.get("storage_encryption_enabled"), default=False)

    def open_overlay():
        if encrypted:
            return None
        return _open_document_path_overlay(root, instance_id)

    return paths, open_overlay


def _local_ntfs(path: Path) -> bool:
    import pywintypes
    import win32api
    import win32file

    try:
        volume = win32file.GetVolumePathName(str(path))
        return (
            win32file.GetDriveType(volume) == win32file.DRIVE_FIXED
            and win32api.GetVolumeInformation(volume)[-1] == "NTFS"
        )
    except pywintypes.error as exc:
        raise OSError(exc.winerror, exc.strerror) from exc


def _document_overlay_root(config: Mapping[str, Any] | Config | None) -> Path | None:
    ctx = _local_document_context(config)
    if ctx is None or as_bool(ctx.globals.get("storage_encryption_enabled"), default=False):
        return None
    source = ctx.namespaces["config_source"]["default"]
    document = ctx.namespaces["markdown_store"]["default"]
    overrides = source.params.get("values", {})
    if not isinstance(overrides, Mapping):
        return None
    root = overrides.get("markdown_store.root") or document.params.get("root")
    if not isinstance(root, str) or not root or not Path(root).is_absolute():
        return None
    return Path(root)


def open_document_overlay(config: Mapping[str, Any] | Config | None) -> SQLiteOverlayStore | None:
    """仅在固定本地文档根目录来源可确定时返回持久存储。"""
    root = _document_overlay_root(config)
    return _open_document_path_overlay(root, "default") if root is not None else None


def _open_document_path_overlay(root_path: Path, instance_id: str) -> SQLiteOverlayStore | None:
    internal = root_path / "memory" / ".shadow"
    path = internal / "portal-config.db"
    try:
        existed = path.exists()
    except OSError:
        existed = True
    try:
        root_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        internal.mkdir(mode=0o700, parents=True, exist_ok=True)
        return _open_overlay(path, instance_id)
    except OSError as exc:
        if existed:
            raise BackendError("cannot access the instance configuration overlay") from exc
        logger.warning(
            "configuration overlay directory %s is unavailable; configuration is read-only: %s",
            internal,
            exc,
        )
        return None


def _file_overlay_path(
    config: Mapping[str, Any] | Config | None,
    paths: Sequence[str],
) -> Path | None:
    if not paths:
        return None
    ctx = _startup_context(config)
    if as_bool(ctx.globals.get("storage_encryption_enabled"), default=False):
        return None
    source = ctx.namespaces.get("config_source", {}).get("default")
    if source is None or source.target != "yaml_defaults":
        return None
    try:
        resolved = [Path(path).resolve(strict=True) for path in paths]
        if any(not path.is_file() for path in resolved):
            return None
        # 同一组配置层无论从何处启动均指向同一文件；多层顺序也是实例身份的一部分。
        digest = hashlib.sha256("\0".join(map(str, resolved)).encode()).hexdigest()[:20]
        return resolved[-1].parent / f".{resolved[-1].name}.portal-{digest}.db"
    except OSError as exc:
        raise BackendError("cannot access the instance configuration overlay") from exc


def open_file_overlay(
    config: Mapping[str, Any] | Config | None,
    paths: Sequence[str],
) -> SQLiteOverlayStore | None:
    """从启动器提供的配置文件集合推导实例覆盖位置，不修改配置源。"""
    path = _file_overlay_path(config, paths)
    return _open_overlay(path) if path is not None else None


def _config_overlay_candidates(
    config: Mapping[str, Any] | Config | None,
    config_paths: Sequence[str],
) -> Iterator[tuple[Path, Callable[[], SQLiteOverlayStore | None]]]:
    """Yield possible configuration locations without opening or migrating a database."""
    root = _document_overlay_root(config)
    if root is not None:
        yield root / "memory" / ".shadow" / "portal-config.db", partial(
            _open_document_path_overlay, root, "default",
        )
    path = _file_overlay_path(config, config_paths)
    if path is not None:
        yield path, partial(_open_overlay, path)


def open_config_overlay(
    config: Mapping[str, Any] | Config | None,
    config_paths: Sequence[str] = (),
) -> SQLiteOverlayStore | None:
    """优先使用文档根目录，其次使用实际加载的配置文件旁的覆盖库。"""
    for _, open_overlay in _config_overlay_candidates(config, config_paths):
        store = open_overlay()
        if store is not None:
            return store
    return None


def document_llm_instances(config: Mapping[str, Any] | Config | None) -> list[str]:
    """列出配置中声明的具名模型实例；实际可写性仍由运行时装配决定。"""
    return sorted(_startup_context(config).namespaces.get("llm", {}))
