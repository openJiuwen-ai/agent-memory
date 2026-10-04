# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Optional, side-effect-free source-update preparation contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

from jiuwen_memory.common.type_def import MemoryUnit

from .index_builder import IndexBuilder

STRICT_ENTITY_WRITES: ContextVar[bool] = ContextVar("schema_update_strict_entities", default=False)


@dataclass
class SourceExtraction:
    properties: list[MemoryUnit]
    entities: list[str]


@dataclass
class UnitChange:
    before: MemoryUnit | None
    after: MemoryUnit | None


@dataclass
class SourceUpdatePlan:
    operation_id: str
    source_before: MemoryUnit
    source_after: MemoryUnit
    changes: list[UnitChange]


class SourceUpdateSupport(ABC):
    """Capability of a configured Schema evolver; ordinary evolvers do not implement it."""

    @abstractmethod
    def source_schema_identity(self) -> tuple[str, str]:
        """Schema name and version used to interpret source updates."""

    @abstractmethod
    def prepare_source_update(
        self, old: MemoryUnit, new: MemoryUnit, *, mode: str
    ) -> SourceUpdatePlan | None:
        """Return a request-local plan without business writes or persistent state."""

    @abstractmethod
    def commit_source_update(
        self, plan: SourceUpdatePlan, *, index_for: Callable[[MemoryUnit], IndexBuilder]
    ) -> MemoryUnit:
        """Commit only after API authorization of every planned change."""
