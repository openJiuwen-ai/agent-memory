# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""数据面写命令端口：封装 ``MemoryEngine`` 的 add/batch/update/delete/evolve。"""

from __future__ import annotations

from datetime import datetime

from jiuwen_memory.common.type_def import MemoryUnit, MetadataValueType, Modality, Scope
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control.engine import MemoryEngine
from jiuwen_memory.control.types import (
    BatchWriteItem,
    BatchWriteOutcome,
    BatchWriteResult,
    Channel,
    DeleteSelector,
    MemoryPatch,
)


class MemoryCommandService:
    """已鉴权 target scope 上的写编排入口。

    批量拆分、幂等、重试、版本和生命周期规则仍在 Engine / 构建层；本端口只转发
    已通过 PEP 的命令，供 API 与单测在不构造 ``LocalMemoryAPI`` 时复用同一入口。
    """

    def __init__(self, engine: MemoryEngine) -> None:
        self._engine = engine

    async def write(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        return await self._engine.write(
            content,
            scope,
            source,
            assets=assets,
            tags=tags,
            system_metadata=system_metadata,
            user_metadata=user_metadata,
            occurred_at=occurred_at,
        )

    async def batch_write(
        self,
        items: list[BatchWriteItem],
        *,
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        return await self._engine.batch_write(items, continue_on_error=continue_on_error)

    async def batch_write_aligned(
        self,
        engine_items: list[BatchWriteItem],
        origins: list[tuple[int, BatchWriteItem]],
        *,
        continue_on_error: bool = True,
    ) -> list[BatchWriteOutcome]:
        """Write already-authorized items, then restore caller indexes and items.

        Engine items may carry author marks; ``origins`` keep the caller-visible
        item so kernel marks are not echoed as if the caller sent them.
        """
        result = await self._engine.batch_write(
            engine_items, continue_on_error=continue_on_error
        )
        aligned: list[BatchWriteOutcome] = []
        for outcome, (index, item) in zip(result.outcomes, origins):
            outcome.index = index
            outcome.item = item
            aligned.append(outcome)
        return aligned

    @staticmethod
    def collect_batch_result(
        outcomes: dict[int, BatchWriteOutcome], size: int
    ) -> BatchWriteResult:
        return BatchWriteResult(outcomes=[outcomes[index] for index in range(size)])

    async def update(self, unit_id: str, scope: Scope, patch: MemoryPatch) -> MemoryUnit:
        return await self._engine.update(unit_id, scope, patch)

    async def delete(self, selector: DeleteSelector) -> list[str]:
        return await self._engine.delete(selector)

    async def evolve(
        self, scope: Scope, mode: EvolveMode, channel: Channel = Channel.BACKGROUND
    ) -> str:
        return await self._engine.evolve(scope, mode, channel)
