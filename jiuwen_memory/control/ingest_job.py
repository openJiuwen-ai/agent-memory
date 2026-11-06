# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Long-running ingest job control contract."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from threading import Event

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.ingest_cancellation import (
    IngestCancelled as IngestCancelled,
)
from jiuwen_memory.common.ingest_cancellation import (
    check_ingest_cancelled as check_ingest_cancelled,
)
from jiuwen_memory.common.type_def import MemoryUnit, Scope

from .base import ControlOperator

INGEST_JOB_PREFIX = "ing_"
IngestTask = Callable[[], list[MemoryUnit]]


@dataclass
class IngestCancellation:
    requested: Event
    write_started: bool = False
    unit_ids: tuple[str, ...] = ()


# Private execution context: never supplied through metadata or public write arguments.
ingest_cancellation: ContextVar[IngestCancellation | None] = ContextVar(
    "ingest_cancellation", default=None
)


def begin_ingest_write() -> None:
    check_ingest_cancelled()
    state = ingest_cancellation.get()
    if state is not None:
        state.write_started = True


def record_ingest_units(units: list[MemoryUnit]) -> None:
    record_ingest_ids([unit.id for unit in units])


def record_ingest_ids(unit_ids: list[str]) -> None:
    state = ingest_cancellation.get()
    if state is not None:
        state.unit_ids = tuple(dict.fromkeys((*state.unit_ids, *unit_ids)))


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
    cancel_requested: bool = False
    partial_write: bool = False
    retry_requires_reconciliation: bool = False


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
    def cancel(self, job_id: str, *, scope: Scope) -> None:
        """Request cooperative cancellation; terminal jobs are unchanged."""

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停止接收新任务；wait=True 等待全部已提交任务及其状态写入完成。

        wait=False 仅发出停机请求，不代表任务结束；之后的 wait=True 必须仍能等待。
        重复调用幂等，不取消已提交任务。
        """
