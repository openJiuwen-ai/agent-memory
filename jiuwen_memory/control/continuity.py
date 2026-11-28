"""Continuity application contract. Authentication remains at the API boundary."""

import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from jiuwen_memory.common.errors import (
    ITEM_LEVEL_ERRORS,
    UnsupportedCapabilityError,
    ValidationError,
    is_item_level_error,
)
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


class TaskConnectorPolicy(Protocol):
    """Connector governance supplied by the API PEP for task changes.

    The Continuity port calls these checks on the same task record snapshot it
    mutates. Participant checks raise ``PermissionDeniedError``; the owner bind
    check raises ``ValidationError`` with the detailed configuration reason.
    """

    def owner_connector(self, connector_id: str) -> None:
        """Owner bind: connector configured, enabled, org-matched and project-covered."""

    def owned_connector(self, connector_id: str) -> None:
        """Participant attribution: the connector is configured for the caller's actor."""

    def available_connector(self, connector_id: str) -> None:
        """Participant new close: owned, enabled and still covering the project."""

    def actor_connector(self, connector_id: str) -> None:
        """Participant new bind: the caller's single enabled project connector."""


class ContinuityProducer(Factory):
    TOP_NAME = "continuity"

    @classmethod
    def build(cls, target: str, params: Any, ctx: Any, *, name: str = "") -> Any:
        instance = super().build(target, params, ctx, name=name)
        require_task_connector_policy(instance, target)
        return instance


def require_task_connector_policy(plugin: Any, target: str) -> None:
    """Assembly gate: Continuity plugins must accept the commit-time policy (S09 §14)."""
    parameters = inspect.signature(type(plugin).task).parameters
    accepts_policy = any(
        parameter.kind is parameter.VAR_KEYWORD or parameter.name == "connector_policy"
        for parameter in parameters.values()
    )
    if not accepts_policy:
        raise ValidationError(
            f"continuity target {target!r}: {type(plugin).__name__}.task must accept the "
            "connector_policy keyword; task-change authorization runs inside the port"
        )


@dataclass(frozen=True)
class TaskPolicy:
    """``continuity_task`` authorization switches (D13).

    ``self_bind`` defaults on: participants may bind/close only through a connector the
    port re-validates at commit time, which is what SDK / HTTP deployments already do.
    Setting it off restores the strict owner-only rule. ``admin_manage_tasks`` stays off
    so existing deployments keep rejecting space managers without the WRITE grant.
    """

    self_bind: bool = True
    admin_manage_tasks: bool = False


class Continuity(ABC):
    # 部署档位，公开能力声明：``role``（含关系扩展）或 ``continuity``（仅接续）。
    # API 依赖该声明判断能否展开关系，不探测实现私有状态（S09 §1/§6/§7）。
    mode: str = "role"
    # 任务授权策略（D13）：默认关闭，既有部署行为不变；本机模板显式开启。
    task_policy: TaskPolicy = TaskPolicy()

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
    def task(
        self,
        scope: Scope,
        request: TaskRequest,
        owner: str,
        read: ReadMemory,
        *,
        connector_policy: TaskConnectorPolicy,
        managing: bool = False,
    ) -> dict:
        ...

    @abstractmethod
    def task_owner(self, scope: Scope, task_id: str) -> str:
        ...

    @abstractmethod
    def binding_connector(self, scope: Scope, task_id: str, binding_id: str) -> str:
        ...

    def binding(self, scope: Scope, task_id: str, binding_id: str) -> dict:
        """Return one binding record, including inactive ones.

        ``continuity_task``'s self-unbind policy needs the target binding's connector,
        and a revoked binding must not be mistaken for a foreign one. Implementations
        that cannot expose the record keep the closed default.
        """
        raise UnsupportedCapabilityError(
            capability="binding",
            value=binding_id,
            component="continuity",
            message="this continuity implementation cannot expose binding records",
        )

    def event_receipt(
        self, scope: Scope, connector: str, binding_id: str, event_id: str
    ) -> dict | None:
        """Read-only receipt lookup by event key (D8).

        Returns ``{status, operation_id, refs, retry_action}`` or ``None`` when the
        event key was never registered. Never returns content and never changes state;
        the local service uses it to settle ``prepared`` operations after a revocation.
        """
        raise UnsupportedCapabilityError(
            capability="event_receipt",
            value=event_id,
            component="continuity",
            message="this continuity implementation cannot query event receipts",
        )

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
        continue_on_error: bool = False,
    ) -> list[dict]:
        """Reliable batch write: one dict per event, in input order.

        The default implementation keeps plug-ins working by falling back to per-event
        ``record``; implementations that support it batch body writes, append task
        background once per task and bump each task revision once (D5).

        ``continue_on_error`` 是契约的一部分：可归因到单条且需先修正再提交的失败返回
        ``{"status": "rejected", "operation_id": ..., "error": exc}`` 而不是抛错；
        ``continue_on_error=False`` 时其后未执行的项返回 ``{status: skipped}``。
        状态可原样重试的故障（后端不可用、搬迁中等）仍向上抛。回退实现本就逐条执行，
        因此只需把可归因失败降级，不必改变写入顺序。
        """
        results: list[dict] = []
        for index, event in enumerate(events):
            fingerprint_value = (request_fingerprints or [""] * len(events))[index]
            key = self.event_key(connectors[index], event.binding_id, event.event_id)
            try:
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
            except ITEM_LEVEL_ERRORS as exc:
                # 与内置实现同一语义：失败项 rejected；严格模式下后续项 skipped，
                # 而不是抛错让调用方把整段（含已写入的前缀）都记成同一个错误。
                if not is_item_level_error(exc):
                    raise
                results.append({"status": "rejected", "operation_id": key, "error": exc})
                if not continue_on_error:
                    for later in range(index + 1, len(events)):
                        later_key = self.event_key(
                            connectors[later],
                            events[later].binding_id,
                            events[later].event_id,
                        )
                        results.append({"status": "skipped", "operation_id": later_key})
                    break
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
