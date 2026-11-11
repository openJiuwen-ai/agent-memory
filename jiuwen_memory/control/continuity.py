"""Continuity application contract. Authentication remains at the API boundary."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from jiuwen_memory.common.errors import UnsupportedCapabilityError
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
    # 部署档位，公开能力声明：``role``（含关系扩展）或 ``continuity``（仅接续）。
    # API 依赖该声明判断能否展开关系，不探测实现私有状态（S09 §1/§6/§7）。
    mode: str = "role"

    def retains(self, unit) -> bool:
        """Conservative automatic-cleanup guard; implementations must inspect references."""
        return True

    @abstractmethod
    def health(self) -> dict:
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
        raise UnsupportedCapabilityError(
            "quarantine_source", source_key, "continuity",
            "source quarantine is not supported",
        )

    def release_source(self, scope: Scope, source_key: str) -> bool:
        """Release a quarantined source; returns whether it was quarantined."""
        return False

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
        *, query_output=None, request_fingerprint: str = "",
    ) -> dict:
        ...

    def view_result(
        self,
        scope: Scope,
        request: RoleViewQuery,
        read: ReadMemory,
        discover,
        *, top_k: int,
    ) -> dict:
        """Duty view normalized for the API layer: ``{items, paths, gaps, meta}``.

        ``items`` entries are ``{unit_id, text, group, annotation}``; the API layer only
        converts them to its result type. Assembling entries, ordering, truncation gaps
        and meta belongs to the view plug-in, not to the API mixin.
        """
        raise UnsupportedCapabilityError(
            capability="view_result",
            value=request.task_id or request.query,
            component="continuity",
            message="this continuity implementation cannot normalize a duty view",
        )

    def continuity_view_result(
        self,
        scope: Scope,
        request: RoleViewQuery,
        read: ReadMemory,
        *,
        extra_items,
        top_k: int,
    ) -> dict:
        """Continuity-mode view: task background + anchor seeds, no relation expansion.

        ``extra_items`` are the plain-retrieval hits (``{unit_id, content}``); merging,
        ordering, the ``required`` flag and the gaps belong here, not in the API layer.
        """
        raise UnsupportedCapabilityError(
            capability="continuity_view_result",
            value=request.task_id or request.query,
            component="continuity",
            message="this continuity implementation cannot merge a continuity view",
        )

    def event_key(self, connector: str, binding_id: str, event_id: str) -> str:
        """The single idempotency key for one continuity event (all callers share it)."""
        from jiuwen_memory.common.type_def.continuity import fingerprint

        return fingerprint(
            {"connector": connector, "binding": binding_id, "event": event_id}
        )

    def record_batch(
        self,
        scope: Scope,
        events: list[ContinuityEvent],
        connectors: list[str],
        read: ReadMemory,
        write_batch,
        *, request_fingerprints: list[str] | None = None, query_output=None,
    ) -> list[dict]:
        """Reliable batch write: one dict per event, in input order.

        The default implementation keeps plug-ins working by falling back to per-event
        ``record``; implementations that support it batch body writes, append task
        background once per task and bump each task revision once (D5).
        """
        results = []
        for index, event in enumerate(events):
            fingerprint_value = (request_fingerprints or [""] * len(events))[index]
            key = self.event_key(connectors[index], event.binding_id, event.event_id)
            results.append(
                self.record(
                    scope,
                    event,
                    connectors[index],
                    read,
                    self._legacy_writer(write_batch, key),
                    query_output=query_output,
                    request_fingerprint=fingerprint_value,
                )
            )
        return results

    @staticmethod
    def _batch_item(operation_id: str, content: str, metadata: dict, *, derived: bool):
        from jiuwen_memory.common.type_def.continuity import ContinuityBatchWrite

        return ContinuityBatchWrite(
            operation_id=operation_id, content=content, metadata=metadata, derived=derived
        )

    @classmethod
    def _derived_writer(cls, write_batch, operation_id: str):
        """派生输出的写入回调：显式标记 ``derived``，``operation_id`` 指向源事件。"""

        def write(content, metadata):
            batches = write_batch(
                [cls._batch_item(operation_id, content, metadata, derived=True)]
            )
            return list(batches[0]) if batches else []

        return write

    @classmethod
    def _legacy_writer(cls, write_batch, operation_id: str):
        """单条 ``record`` 的回调形状 → 类型化批量条目（仅用于回退实现）。

        ``record`` 用同一个回调写正文和派生输出，这里只能按 metadata 区分两者；
        ``record_batch`` 的真实实现不走这里，派生写入用 ``_derived_writer``。
        """

        def write(content, metadata):
            derived = "continuity_output" in (metadata or {})
            batches = write_batch(
                [cls._batch_item(operation_id, content, metadata, derived=derived)]
            )
            return list(batches[0]) if batches else []

        return write

    @abstractmethod
    def relation(
        self,
        scope: Scope,
        relation: RelationRecord,
        expected_revision: int,
        read: ReadMemory,
        read_previous: ReadMemory | None = None,
    ) -> dict:
        ...

    @abstractmethod
    def relation_current(self, scope: Scope, relation_id: str) -> dict | None:
        ...

    @abstractmethod
    def validate_resolution(
        self, scope: Scope, relation: RelationRecord, read: ReadMemory
    ) -> None:
        ...

    def task_background(self, scope: Scope, task_id: str, read: ReadMemory) -> dict:
        """Task background as ``{units, required_ids, gaps}``.

        Continuity mode has no relation expansion but must still restore the task's
        background, so this stays a public capability rather than a role-mode-only path.
        Implementations must report missing/unreadable background as ``gaps`` (with the
        stored ``required`` flag) instead of returning an empty success: "no background"
        and "a required premise is unreadable" are different answers to the caller.
        """
        raise UnsupportedCapabilityError(
            capability="task_background",
            value=task_id,
            component="continuity",
            message="this continuity implementation cannot restore task background",
        )

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
