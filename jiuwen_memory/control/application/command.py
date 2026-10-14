# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""数据面写命令端口：封装 ``MemoryEngine`` 的 add/batch/update/delete/evolve。"""

from __future__ import annotations

from datetime import datetime

from jiuwen_memory.common.security.types import Action
from jiuwen_memory.common.type_def import (
    CandidateSource,
    MemoryUnit,
    MetadataValueType,
    Modality,
    Scope,
)
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.construction.source_update import SourceUpdatePlan
from jiuwen_memory.control.engine import MemoryEngine
from jiuwen_memory.control.types import (
    BatchWriteItem,
    BatchWriteOutcome,
    BatchWriteResult,
    Channel,
    DeleteSelector,
    MemoryPatch,
    PermissionContext,
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
        result = await self._engine.batch_write(engine_items, continue_on_error=continue_on_error)
        aligned: list[BatchWriteOutcome] = []
        for outcome, (index, item) in zip(result.outcomes, origins):
            outcome.index = index
            outcome.item = item
            aligned.append(outcome)
        return aligned

    @staticmethod
    def collect_batch_result(outcomes: dict[int, BatchWriteOutcome], size: int) -> BatchWriteResult:
        return BatchWriteResult(outcomes=[outcomes[index] for index in range(size)])

    async def update(self, unit_id: str, scope: Scope, patch: MemoryPatch) -> MemoryUnit:
        return await self._engine.update(unit_id, scope, patch)

    def requires_update_preparation(self, unit: MemoryUnit, patch: MemoryPatch) -> bool:
        return self._engine.requires_update_preparation(unit, patch)

    async def prepare_update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch
    ) -> SourceUpdatePlan | None:
        return await self._engine.prepare_update(unit_id, scope, patch)

    async def commit_update(self, plan: SourceUpdatePlan) -> MemoryUnit:
        return await self._engine.commit_update(plan)

    @staticmethod
    def update_permission_contexts(
        plan: SourceUpdatePlan,
    ) -> list[tuple[Action, PermissionContext]]:
        """Expose both original and prospective routing metadata to the API PEP."""
        result = []
        for change in plan.changes:
            action = (
                Action.DELETE
                if change.after is None
                else Action.WRITE
                if change.before is None
                else Action.UPDATE
            )
            if change.after is not None and (
                change.after.id == plan.source_after.id or change.after.supersedes
            ):
                action = Action.UPDATE
            for unit in (change.before, change.after):
                if unit is None:
                    continue
                result.append(
                    (
                        action,
                        PermissionContext(
                            resource_type="memory_unit",
                            scope=unit.scope,
                            unit_id=unit.id,
                            memory_type=str(unit.system_metadata.get("memory_type", "")).strip(),
                            pipeline=str(unit.system_metadata.get("pipeline", "")).strip(),
                            tags=tuple(unit.tags),
                            metadata={
                                key: str(value) for key, value in unit.system_metadata.items()
                            },
                        ),
                    )
                )
        return result

    async def delete(self, selector: DeleteSelector) -> list[str]:
        return await self._engine.delete(selector)

    async def evolve(
        self,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel = Channel.BACKGROUND,
        *,
        candidate: CandidateSource | dict | None = None,
        buckets: list[Scope] | None = None,
        denied_scopes: list[str] | None = None,
    ) -> str | None:
        """提交一次性 EvolveJob（纯执行链）。

        ``buckets`` / ``denied_scopes`` 为 API 层 fan-out 逐桶裁决的产物
        （PEP 边界：鉴权在 API 层，本端口只转发已获准的命令）。
        """
        return await self._engine.evolve(
            scope,
            mode,
            channel,
            candidate=candidate,
            buckets=buckets,
            denied_scopes=denied_scopes,
        )
