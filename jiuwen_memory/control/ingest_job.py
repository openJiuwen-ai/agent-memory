# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Long-running ingest job control contract."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, Scope

from .base import ControlOperator

INGEST_JOB_PREFIX = "ing_"
IngestTask = Callable[[], list[MemoryUnit]]


@dataclass(frozen=True)
class IngestJob:
    id: str
    payload_id: str
    source_ref: str
    scope: Scope
    status: str
    created_at: datetime
    updated_at: datetime
    unit_ids: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class IngestSubmission:
    job: IngestJob
    reused: bool


class IngestJobProducer(Factory):
    """Factory for long-running ingest job controllers."""

    TOP_NAME = "ingest_job"


class IngestJobController(ControlOperator):
    """Queue, persist and query long-running ingest jobs."""

    @abstractmethod
    def submit(
        self,
        *,
        payload_id: str,
        source_ref: str,
        scope: Scope,
        task: IngestTask,
    ) -> IngestSubmission:
        """Submit or reuse an ingest job."""

    @abstractmethod
    def status(self, job_id: str, *, scope: Scope) -> IngestJob:
        """Return a job only when it belongs to the requested scope."""

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """Release worker resources."""
