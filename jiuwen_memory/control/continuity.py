"""Continuity application contract. Authentication remains at the API boundary."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    ContinuityRecovery,
    MemoryRef,
    RelationRecord,
    RoleViewQuery,
    TaskRequest,
)

ReadMemory = Callable[[MemoryRef], MemoryUnit]
WriteMemory = Callable[[str, dict[str, Any]], list[MemoryUnit]]


class ContinuityProducer(Factory):
    TOP_NAME = "continuity"


class Continuity(ABC):
    def retains(self, unit) -> bool:
        """Conservative automatic-cleanup guard; implementations must inspect references."""
        return True

    @abstractmethod
    def health(self) -> None:
        ...

    def close(self) -> None:
        """Release owned storage resources (graph store, registry KV and its single-writer lock).

        Default no-op; the assembly root owns the resources and closes them once, in
        admission-stop → continuity → graph store → underlying KV order.
        """

    def source_quarantine(self, scope: Scope, source_key: str) -> dict | None:
        """Return the quarantine record for ``source_key``, or ``None`` if not quarantined.

        Default: no source is quarantined. Implementations persist the governed source
        state and return ``{source_key, reason, operator, quarantined_at}``.
        """
        return None

    def quarantine_source(
        self, scope: Scope, source_key: str, reason: str, *, operator: str = ""
    ) -> dict:
        """Quarantine a source range with an auditable non-empty reason."""
        from jiuwen_memory.common.errors import UnsupportedCapabilityError

        raise UnsupportedCapabilityError("source quarantine is not supported")

    def release_source(self, scope: Scope, source_key: str) -> bool:
        """Release a quarantined source; returns whether it was quarantined."""
        return False

    def mark_source_cutover(self, scope: Scope, connector: str, context_id: str) -> dict:
        """Cut a dual-write source over to agent-memory as the single authoritative writer."""
        from jiuwen_memory.common.errors import UnsupportedCapabilityError

        raise UnsupportedCapabilityError("source cutover is not supported")

    def source_cutover(self, scope: Scope, connector: str, context_id: str) -> int | None:
        """Return the cutover watermark position for an ordered source, or ``None``."""
        return None

    @abstractmethod
    def task(self, scope: Scope, request: TaskRequest, owner: str, read: ReadMemory) -> dict:
        ...

    @abstractmethod
    def task_owner(self, scope: Scope, task_id: str) -> str:
        ...

    @abstractmethod
    def binding_connector(self, scope: Scope, task_id: str, binding_id: str) -> str:
        ...

    @abstractmethod
    def record(
        self,
        scope: Scope,
        event: ContinuityEvent,
        connector: str,
        read: ReadMemory,
        write: WriteMemory,
        *, query_output=None, conditional_update=None,
    ) -> dict:
        ...

    @abstractmethod
    def relation(
        self,
        scope: Scope,
        relation: RelationRecord,
        expected_revision: int,
        read: ReadMemory,
        write: WriteMemory,
        read_previous: ReadMemory | None = None,
    ) -> dict:
        ...

    @abstractmethod
    def relation_head_ref(self, scope: Scope, relation_id: str) -> dict | None:
        ...

    @abstractmethod
    def validate_resolution(
        self, scope: Scope, relation: RelationRecord, read: ReadMemory
    ) -> None:
        ...

    @abstractmethod
    def view(
        self,
        scope: Scope,
        request: RoleViewQuery,
        read: ReadMemory,
        discover: Callable[[str, int], list[MemoryUnit]],
    ) -> dict:
        ...

    @abstractmethod
    def repair(self, scope: Scope, read: ReadMemory) -> dict:
        ...

    @abstractmethod
    def recovery_owner(self, scope: Scope, request: ContinuityRecovery) -> str:
        ...

    @abstractmethod
    def recover(self, scope: Scope, request: ContinuityRecovery, read: ReadMemory) -> dict:
        ...
