# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""从可信的启动位置定位本地配置覆盖库。"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.config.config import Config
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.config.overlay_store import SQLiteOverlayStore

logger = get_logger(__name__)


def _open_overlay(path: Path) -> SQLiteOverlayStore | None:
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
        return SQLiteOverlayStore(path)
    except (OSError, sqlite3.Error) as exc:
        if existed:
            raise BackendError("cannot access the instance configuration overlay") from exc
        logger.warning(
            "configuration overlay at %s cannot be created; configuration is read-only: %s",
            path,
            exc,
        )
        return None


def open_document_overlay(config: Mapping[str, Any] | Config | None) -> SQLiteOverlayStore | None:
    """仅在固定本地文档根目录来源可确定时返回持久存储。"""
    ctx = default_context()
    if config is not None:
        supplied = config if isinstance(config, Config) else Config.from_dict(config)
        ctx = ctx.merged(supplied.context())
    if not as_bool(ctx.globals.get("write_document"), default=False):
        return None
    if as_bool(ctx.globals.get("storage_encryption_enabled"), default=False):
        return None
    source = ctx.namespaces.get("config_source", {}).get("default")
    document = ctx.namespaces.get("markdown_store", {}).get("default")
    if source is None or source.target != "yaml_defaults" or document is None:
        return None
    if document.target != "local":
        return None
    overrides = source.params.get("values", {})
    if not isinstance(overrides, Mapping):
        return None
    root = overrides.get("markdown_store.root") or document.params.get("root")
    if not isinstance(root, str) or not root or not Path(root).is_absolute():
        return None
    root_path = Path(root)
    internal = root_path / "memory" / ".shadow"
    path = internal / "portal-config.db"
    try:
        existed = path.exists()
    except OSError:
        existed = True
    try:
        root_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        internal.mkdir(mode=0o700, parents=True, exist_ok=True)
        return _open_overlay(path)
    except OSError as exc:
        if existed:
            raise BackendError("cannot access the instance configuration overlay") from exc
        logger.warning(
            "configuration overlay directory %s is unavailable; configuration is read-only: %s",
            internal,
            exc,
        )
        return None


def open_file_overlay(
    config: Mapping[str, Any] | Config | None,
    paths: Sequence[str],
) -> SQLiteOverlayStore | None:
    """从启动器提供的配置文件集合推导实例覆盖位置，不修改配置源。"""
    if not paths:
        return None
    ctx = default_context()
    if config is not None:
        supplied = config if isinstance(config, Config) else Config.from_dict(config)
        ctx = ctx.merged(supplied.context())
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
        path = resolved[-1].parent / f".{resolved[-1].name}.portal-{digest}.db"
        return _open_overlay(path)
    except OSError as exc:
        raise BackendError("cannot access the instance configuration overlay") from exc


def open_config_overlay(
    config: Mapping[str, Any] | Config | None,
    config_paths: Sequence[str] = (),
) -> SQLiteOverlayStore | None:
    """优先使用文档根目录，其次使用实际加载的配置文件旁的覆盖库。"""
    return open_document_overlay(config) or open_file_overlay(config, config_paths)


def document_llm_instances(config: Mapping[str, Any] | Config | None) -> list[str]:
    """列出配置中声明的具名模型实例；实际可写性仍由运行时装配决定。"""
    ctx = default_context()
    if config is not None:
        supplied = config if isinstance(config, Config) else Config.from_dict(config)
        ctx = ctx.merged(supplied.context())
    return sorted(ctx.namespaces.get("llm", {}))
