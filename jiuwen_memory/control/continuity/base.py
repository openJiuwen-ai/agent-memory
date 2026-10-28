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
    @abstractmethod
    def health(self) -> None: ...

    @abstractmethod
    def task(self, scope: Scope, request: TaskRequest, owner: str, read: ReadMemory) -> dict: ...

    @abstractmethod
    def task_owner(self, scope: Scope, task_id: str) -> str: ...

    @abstractmethod
    def binding_connector(self, scope: Scope, task_id: str, binding_id: str) -> str: ...

    @abstractmethod
    def record(
        self,
        scope: Scope,
        event: ContinuityEvent,
        connector: str,
        read: ReadMemory,
        write: WriteMemory,
    ) -> dict: ...

    @abstractmethod
    def relation(
        self,
        scope: Scope,
        relation: RelationRecord,
        expected_revision: int,
        read: ReadMemory,
        write: WriteMemory,
        read_previous: ReadMemory | None = None,
    ) -> dict: ...

    @abstractmethod
    def relation_head_ref(self, scope: Scope, relation_id: str) -> dict | None: ...

    @abstractmethod
    def validate_resolution(
        self, scope: Scope, relation: RelationRecord, read: ReadMemory
    ) -> None: ...

    @abstractmethod
    def view(
        self,
        scope: Scope,
        request: RoleViewQuery,
        read: ReadMemory,
        discover: Callable[[str, int], list[MemoryUnit]],
    ) -> dict: ...

    @abstractmethod
    def repair(self, scope: Scope, read: ReadMemory) -> dict: ...

    @abstractmethod
    def recovery_owner(self, scope: Scope, request: ContinuityRecovery) -> str: ...

    @abstractmethod
    def recover(self, scope: Scope, request: ContinuityRecovery, read: ReadMemory) -> dict: ...
