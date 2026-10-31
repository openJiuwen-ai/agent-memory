# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Space 删除事务：purge 记忆真源/索引后再删 space 元数据，并汇总计数。"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from jiuwen_memory.common.errors import NotFoundError, PartialFailureError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.engine import MemoryEngine
from jiuwen_memory.control.space import SpaceManager
from jiuwen_memory.control.types import SpaceDeleteResult

logger = get_logger(__name__)

# 删除后回调的签名：(org, space, owners) -> None。owners 是被删空间的归属主体
# 登记元数据（F07），在元数据删除**之前**读出——删除后就读不到了。
OnSpaceDeleted = Callable[[str, str, list[Scope]], None]


class SpaceLifecycleService:
    """``delete_space`` 在鉴权之后的两步事务。

    顺序：先 ``SpaceManager.begin_delete`` 把状态标为 ``DELETING``（阻断新写），
    再 ``MemoryEngine.purge_space``，成功后再 ``SpaceManager.delete``；把
    purge 条数累加进 ``deleted_counts`` 的 ``memory`` / ``index`` / ``kv``。
    purge 失败则不删 space（状态保持 ``DELETING``）。purge 成功而 metadata
    delete 失败时抛 :class:`PartialFailureError`（``retry_action=delete_space``）：
    记忆可能已清除、space 元数据仍在且为 ``DELETING``，重试同一入口——
    ``begin_delete`` / purge 对空或已删除中的空间幂等，第二步再删元数据。
    membership 缓存失效与入口审计仍由 API 在返回后执行。
    Space 的普通 CRUD 不经本服务。

    删除完成后调用 ``on_deleted`` 回调组（mem2.0 §2.5.2「按作者清除」的登记
    路径等）：缺省为空，即现有行为。回调失败不抛出——space.delete 对已删空间
    不幂等（``_require_space`` 抛 NotFoundError），回调异常上抛会让调用方对
    已完成的事务无限重试；标记类操作幂等，记 ``ERROR`` 日志由部署侧补登记。
    """

    def __init__(
        self,
        engine: MemoryEngine,
        space: SpaceManager,
        on_deleted: Sequence[OnSpaceDeleted] = (),
    ) -> None:
        self._engine = engine
        self._space = space
        self._on_deleted = list(on_deleted)

    def _begin_delete(self, org: str, space: str) -> None:
        try:
            self._space.begin_delete(org, space)
        except NotFoundError:
            return

    def _owners_of(self, org: str, space: str) -> list[Scope]:
        """读被删空间的归属主体登记（元数据删除前的最后机会）。"""
        try:
            info = self._space.get(org, space)
        except NotFoundError:
            return []
        return list(info.owners)

    def _notify_deleted(self, org: str, space: str, owners: list[Scope]) -> None:
        for callback in self._on_deleted:
            try:
                callback(org, space, owners)
            except Exception:  # noqa: BLE001 —— 见类 docstring：回调失败不上抛
                logger.exception(
                    "space delete callback failed after successful delete: "
                    "org=%s space=%s", org, space,
                )

    async def delete_space(self, org: str, space: str) -> tuple[SpaceDeleteResult, list[str]]:
        self._begin_delete(org, space)
        owners = self._owners_of(org, space)
        purged = await self._engine.purge_space(org, space)
        try:
            result = self._space.delete(org, space)
        except PartialFailureError:
            raise
        except Exception as exc:
            raise PartialFailureError(
                completed=("purge_space",),
                failed="space.delete",
                retry_action="delete_space",
                message=(
                    f"space {org}/{space} memories purged; metadata delete failed; "
                    f"retry delete_space: {exc}"
                ),
            ) from exc
        added = len(purged)
        result.deleted_counts["memory"] = result.deleted_counts.get("memory", 0) + added
        result.deleted_counts["index"] = result.deleted_counts.get("index", 0) + added
        result.deleted_counts["kv"] = result.deleted_counts.get("kv", 0) + added
        self._notify_deleted(org, space, owners)
        return result, purged
