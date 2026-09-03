# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Control-owned persistence contract for long-running job state.

Job state is Control infrastructure rather than Memory Storage data.  The
contract therefore keeps its own scope/owner checks and retention operations;
one implementation may still use a KV backend underneath.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope

if TYPE_CHECKING:
    from .ingest_job import IngestJob


class JobStateStoreProducer(Factory):
    """Factory for Control-owned job-state stores."""

    TOP_NAME = "job_state_store"


class JobStateStore(ABC):
    """Persist and query long-running jobs within an explicit target Scope.

    ``owner`` is optional during the compatibility period.  When present, an
    implementation must reject a mismatched owner without returning the job.
    """

    @abstractmethod
    def save(
        self,
        job: IngestJob,
        *,
        scope: Scope | None = None,
        owner: Scope | None = None,
    ) -> None:
        """Create or replace a job and its payload-idempotency mapping."""

    @abstractmethod
    def get(
        self,
        job_id: str,
        *,
        scope: Scope,
        owner: Scope | None = None,
    ) -> IngestJob | None:
        """Read one job only from the requested Scope."""

    @abstractmethod
    def find_by_payload(
        self,
        payload_id: str,
        *,
        scope: Scope,
        owner: Scope | None = None,
    ) -> IngestJob | None:
        """Resolve the current payload-idempotency mapping in one Scope."""

    @abstractmethod
    def delete(
        self,
        job_id: str,
        *,
        scope: Scope,
        owner: Scope | None = None,
    ) -> None:
        """Delete a job and its mapping when it still points at that job."""

    @abstractmethod
    def cleanup(
        self,
        scope: Scope,
        *,
        older_than: datetime | None = None,
        owner: Scope | None = None,
    ) -> int:
        """Remove expired/old records in one Scope and return jobs removed."""

    def put(
        self,
        job: IngestJob,
        *,
        scope: Scope | None = None,
        owner: Scope | None = None,
    ) -> None:
        """Compatibility alias for :meth:`save`."""
        self.save(job, scope=scope, owner=owner)

    def find(
        self,
        payload_id: str,
        *,
        scope: Scope,
        owner: Scope | None = None,
    ) -> IngestJob | None:
        """Compatibility alias for :meth:`find_by_payload`."""
        return self.find_by_payload(payload_id, scope=scope, owner=owner)

    def health(self) -> None:
        """Probe the backing infrastructure when applicable."""
        return None
