# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Optional Schema update orchestration shared by local and cloud engines."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.source_update import SourceUpdatePlan, SourceUpdateSupport
from jiuwen_memory.control.types import MemoryPatch, UpdateMode


def _support(engine, unit: MemoryUnit) -> SourceUpdateSupport | None:
    binding = engine._write_binding([unit])
    evolver = binding.evolver if binding is not None else engine._evolver
    return evolver if isinstance(evolver, SourceUpdateSupport) else None


def is_schema_update_candidate(unit: MemoryUnit, patch: MemoryPatch) -> bool:
    """Cheap eligibility check on an already-loaded unit; no routing, copying or I/O."""
    return (
        patch.content is not None
        and unit.system_metadata.get("schema_source_evidence") is True
        and unit.system_metadata.get("extraction_mode") != "schema"
    )


async def prepare_schema_update(
    engine, old: MemoryUnit, new: MemoryUnit, patch: MemoryPatch
) -> SourceUpdatePlan | None:
    if not is_schema_update_candidate(old, patch):
        return None
    support = _support(engine, old)
    if support is None:
        if patch.content != old.content:
            raise ValidationError("The source's Schema pipeline is no longer configured")
        return None
    if patch.content != old.content:
        destination = _support(engine, new)
        if (
            destination is None
            or destination.source_schema_identity() != support.source_schema_identity()
        ):
            raise ValidationError(
                "Schema content update requires the same schema at its destination"
            )
    if patch.mode == UpdateMode.SUPERSEDE and patch.t_valid is None:
        new.temporal.t_valid = datetime.now(timezone.utc)
    request_key = hashlib.sha256(
        json.dumps(asdict(patch), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return await asyncio.to_thread(
        support.prepare_source_update, old, new, mode=patch.mode.value, request_key=request_key
    )


async def commit_schema_update(engine, plan: SourceUpdatePlan) -> MemoryUnit:
    support = _support(engine, plan.source_before)
    if support is None:
        raise ValidationError("The source's Schema pipeline is no longer configured")

    def index_for(unit):
        binding = engine._write_binding([unit])
        return binding.index_builder if binding is not None else engine._index

    return await asyncio.to_thread(support.commit_source_update, plan, index_for=index_for)
