# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Space 删除事务：purge 记忆真源/索引后再删 space 元数据，并汇总计数。"""

from __future__ import annotations

from jiuwen_memory.common.errors import NotFoundError, PartialFailureError
from jiuwen_memory.control.engine import MemoryEngine
from jiuwen_memory.control.space import SpaceManager
from jiuwen_memory.control.types import SpaceDeleteResult


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
    """

    def __init__(self, engine: MemoryEngine, space: SpaceManager) -> None:
        self._engine = engine
        self._space = space

    def _begin_delete(self, org: str, space: str) -> None:
        try:
            self._space.begin_delete(org, space)
        except NotFoundError:
            return

    async def delete_space(self, org: str, space: str) -> tuple[SpaceDeleteResult, list[str]]:
        self._begin_delete(org, space)
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
        return result, purged
