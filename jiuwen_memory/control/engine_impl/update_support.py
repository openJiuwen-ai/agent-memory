# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""三 Engine 共用的单条 update 活跃态前置校验与 SUPERSEDE 分步回滚补偿。

单条 ``update``（含 ``SUPERSEDE`` 与 ``OVERWRITE``）只能作用于当前有效的
``ACTIVE`` 记录：对 ``SUPERSEDED`` 历史版本再次 ``SUPERSEDE`` 会从旧版分叉出
第二个 ``ACTIVE`` 现行版本；对 ``ARCHIVED`` / ``FORGOTTEN`` 记录更新会破坏终态
与只读冷态语义。校验必须在任何写入（含 ``index.build`` 与 Schema 抽取）之前执行；
``SUPERSEDE`` 路径按真源提交进度分步补偿，避免构建中途失败或并发废旧失败留下双
``ACTIVE`` 孤儿，同时避免旧版已废止后二级索引刷新失败误删唯一现行新版。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from jiuwen_memory.common.errors import ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.time_utils import as_utc
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Scope

logger = get_logger(__name__)

_DOC_FINALIZE_TABLE_LOCK = threading.Lock()
_DOC_FINALIZE_LOCKS: dict[tuple[int, str, str, str, str, str, str], list[Any]] = {}


@contextmanager
def doc_supersede_lock(owner: object, scope: Scope, unit_id: str) -> Iterator[None]:
    """在同一存储实例内按 ``(scope, unit_id)`` 串行化文档模式废旧临界区。

    仅包住「重读旧版 → 复核 ACTIVE → 写回 SUPERSEDED」，不包住新版 ``build`` 与回滚，
    避免持锁做重 I/O 或与构建期回调交错死锁；引用计数归零即回收锁项，无内存泄漏。
    """
    key = (id(owner), scope.org, scope.space, scope.user, scope.agent, scope.session, unit_id)
    with _DOC_FINALIZE_TABLE_LOCK:
        entry = _DOC_FINALIZE_LOCKS.get(key)
        if entry is None:
            entry = [threading.Lock(), 0]
            _DOC_FINALIZE_LOCKS[key] = entry
        entry[1] += 1
        lock: threading.Lock = entry[0]
    try:
        with lock:
            yield
    finally:
        with _DOC_FINALIZE_TABLE_LOCK:
            entry[1] -= 1
            if entry[1] == 0:
                _DOC_FINALIZE_LOCKS.pop(key, None)


def ensure_unit_active_for_update(unit: MemoryUnit) -> None:
    """要求待更新记录处于 ACTIVE 态；否则抛 ConflictError（HTTP 409）。"""
    if unit.lifecycle is not LifecycleState.ACTIVE:
        raise ConflictError(
            "memory_unit",
            unit.id,
            message=(
                f"memory unit {unit.id!r} cannot be updated in lifecycle "
                f"{unit.lifecycle.value!r}; only active records can be updated"
            ),
        )


def build_new_or_rollback(
    *,
    new_id: str,
    build_new: Callable[[], None],
    rollback_new: Callable[[], None],
) -> None:
    """写入新版本体与索引；若构建中途失败，清理可能已落盘的新版避免残留双 active。"""
    try:
        build_new()
    except Exception:
        try:
            rollback_new()
        except Exception:
            logger.warning("Engine.update build rollback failed: new_id=%s", new_id)
        raise


def should_rollback_after_supersede_error(
    loader: Callable[[Scope, str], MemoryUnit],
    scope: Scope,
    old_id: str,
    expected_invalid_at: datetime | None,
) -> bool:
    """判断废旧异常后是否应回滚新版：仅当旧版确已由本次更新废止时跳过回滚。"""
    try:
        latest = loader(scope, old_id)
    except NotFoundError:
        return True
    if latest.lifecycle is not LifecycleState.SUPERSEDED:
        return True
    if expected_invalid_at is None or latest.temporal.t_invalid is None:
        return True
    return as_utc(latest.temporal.t_invalid) != as_utc(expected_invalid_at)


def finalize_supersede_or_rollback(
    *,
    new_unit: MemoryUnit,
    finalize_old: Callable[[], Any],
    rollback_new: Callable[[], None],
    map_validation_error: bool = True,
    should_rollback: Callable[[], bool] | None = None,
) -> Any:
    """在 ``new_unit`` 已写入后废止旧版真源；仅当旧版未由本次更新废止时回滚 ``new_unit``。

    ``map_validation_error`` 仅在直接调用 ``lifecycle.supersede`` 的路径开启，避免把
    索引层抛出的普通 ``ValidationError`` 误译为生命周期冲突。
    """
    old_id = new_unit.supersedes or ""
    try:
        return finalize_old()
    except Exception as exc:
        if should_rollback is None or isinstance(exc, ConflictError):
            need_rollback = True
        else:
            try:
                need_rollback = should_rollback()
            except Exception:
                logger.warning(
                    "Engine.update rollback check failed: old_id=%s new_id=%s",
                    old_id,
                    new_unit.id,
                )
                need_rollback = False
        if need_rollback:
            try:
                rollback_new()
            except Exception:
                logger.warning(
                    "Engine.update supersede rollback failed: old_id=%s new_id=%s",
                    old_id,
                    new_unit.id,
                )
        if map_validation_error and isinstance(exc, ValidationError):
            raise ConflictError(
                "memory_unit",
                old_id,
                message=f"memory unit {old_id!r} is no longer active",
            ) from exc
        raise
