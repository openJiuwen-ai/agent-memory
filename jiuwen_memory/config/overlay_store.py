# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""版本化配置覆盖持久化原语。

此模块不选择实例路径，也不提供字段授权。装配方须先确定稳定的实例存储位置并在上层
校验白名单和秘密引用；无可靠位置时不得将内存存储当作持久化后端。
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiuwen_memory.common.errors import ConflictError, ValidationError


@dataclass(frozen=True)
class OverlaySnapshot:
    """某个持久版本的完整覆盖集合。"""

    version: int
    values: dict[str, Any] = field(repr=False)


class SQLiteOverlayStore:
    """使用事务比较版本并整版替换；路径由可信装配方从实例位置确定。"""

    def __init__(self, path: str | Path) -> None:
        path_text = str(path)
        if not path_text or not Path(path_text).is_absolute():
            raise ValidationError("an absolute persistent overlay file is required")
        try:
            descriptor = os.open(path_text, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        info = os.lstat(path_text)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("configuration overlay must be a regular file")
        if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.geteuid()):
            raise ValidationError("configuration overlay must be private to the current user")
        self._path = path_text
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS config_overlay "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL, "
                "payload TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO config_overlay (id, version, payload) VALUES (1, 0, '{}')"
            )

    def credential_store(self):
        """Credentials use the same private database, with existing envelope encryption."""
        from jiuwen_memory.config.model_credentials import ModelCredentials

        return ModelCredentials(self._path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=30)

    def read(self) -> OverlaySnapshot:
        with closing(self._connect()) as db:
            row = db.execute("SELECT version, payload FROM config_overlay WHERE id = 1").fetchone()
        if row is None:
            raise ValidationError("configuration overlay record is missing")
        values = json.loads(row[1])
        if not isinstance(values, dict):
            raise ValidationError("configuration overlay must be an object")
        return OverlaySnapshot(row[0], values)

    def commit(self, expected_version: int, values: Mapping[str, Any]) -> OverlaySnapshot:
        """仅版本相等时持久化完整新快照；异常或冲突不修改旧快照。"""
        if type(expected_version) is not int or expected_version < 0:
            raise ValidationError("expected version must be a non-negative integer")
        if any(not isinstance(key, str) for key in values):
            raise ValidationError("configuration overlay keys must be strings")
        try:
            payload = json.dumps(dict(values), ensure_ascii=False, allow_nan=False)
            normalized = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise ValidationError("configuration overlay must contain JSON values") from exc
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE config_overlay SET version = version + 1, payload = ? "
                "WHERE id = 1 AND version = ?",
                (payload, expected_version),
            )
            if cursor.rowcount != 1:
                raise ConflictError(message="configuration overlay version changed")
        return OverlaySnapshot(expected_version + 1, normalized)


@dataclass(frozen=True)
class OverlaySaveResult:
    """持久化成功与运行发布是两个独立状态。"""

    snapshot: OverlaySnapshot
    applied: bool
