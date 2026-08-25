"""最小实现：:class:`~control.lifecycle.LifecycleManager`。

非破坏式状态流转：把记忆单元在真源里改 ``lifecycle``（active→superseded/archived/
forgotten），不物理删除。``sweep`` 扫描到期（``t_invalid`` 已过）的 active 单元
和 superseded 旧版本，标记 FORGOTTEN，返回被处理的 id。真源读注入的
:class:`~storage.kv.KVStore`
（``scopes()`` + ``scan()`` 跨 scope 扫描，字节经 memory_codec 编解码）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from jiuwen_memory.common.errors import NotFoundError, PolicyError, ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Scope
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.lifecycle import LifecycleManager, LifecycleProducer
from jiuwen_memory.control.policy import PolicyManager, PolicyProducer
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import IndexWriteMode

logger = get_logger(__name__)

_EXPIRED_ACTIVE_TARGET_KEY = "lifecycle.expired_active.target"
_SUPERSEDED_TARGET_KEY = "lifecycle.superseded.target"
_DEFAULT_SWEEP_TARGET = LifecycleState.FORGOTTEN
_POLICY_TARGETS = {
    LifecycleState.FORGOTTEN.value: LifecycleState.FORGOTTEN,
    LifecycleState.ARCHIVED.value: LifecycleState.ARCHIVED,
}


_ALLOWED_TRANSITIONS = {
    LifecycleState.ACTIVE: {
        LifecycleState.ACTIVE,
        LifecycleState.ARCHIVED,
        LifecycleState.FORGOTTEN,
        LifecycleState.SUPERSEDED,
    },
    LifecycleState.ARCHIVED: {
        LifecycleState.ARCHIVED,
        LifecycleState.FORGOTTEN,
    },
    LifecycleState.SUPERSEDED: {
        LifecycleState.SUPERSEDED,
        LifecycleState.FORGOTTEN,
    },
    LifecycleState.FORGOTTEN: {
        LifecycleState.FORGOTTEN,
    },
}


def _ensure_transition_allowed(
    current: LifecycleState, target: LifecycleState, unit_id: str
) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValidationError(
            f"invalid lifecycle transition for {unit_id!r}: {current.value} -> {target.value}"
        )


def _policy_target(policy: PolicyManager | None, key: str) -> LifecycleState:
    if policy is None:
        return _DEFAULT_SWEEP_TARGET
    raw = policy.get(key)
    if raw not in _POLICY_TARGETS:
        allowed = ", ".join(sorted(_POLICY_TARGETS))
        logger.warning(
            "Lifecycle.policy invalid target: key=%s value=%s allowed=%s",
            key,
            raw,
            allowed,
        )
        raise PolicyError(
            f"invalid lifecycle sweep target for {key!r}: {raw!r}; allowed: {allowed}"
        )
    return _POLICY_TARGETS[raw]


def _sweep_target(
    unit: MemoryUnit, now: datetime, policy: PolicyManager | None
) -> LifecycleState | None:
    if unit.lifecycle == LifecycleState.SUPERSEDED:
        return _policy_target(policy, _SUPERSEDED_TARGET_KEY)
    t_invalid = unit.temporal.t_invalid
    if unit.lifecycle == LifecycleState.ACTIVE and t_invalid is not None and t_invalid < now:
        return _policy_target(policy, _EXPIRED_ACTIVE_TARGET_KEY)
    return None


class KVLifecycleManager(LifecycleManager):
    """在 kv 真源上做非破坏式状态流转与到期清扫。"""

    def __init__(self, storage: Storage, policy: PolicyManager | None = None) -> None:
        self._storage = storage
        self._policy = policy

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def transition(
        self, scope: Scope, unit_ids: list[str], target: LifecycleState
    ) -> None:
        matches = self._storage.get(scope, unit_ids)
        for unit in matches:
            _ensure_transition_allowed(unit.lifecycle, target, unit.id)
            unit.lifecycle = target
        if matches:
            self._storage.update(scope, matches, mode=IndexWriteMode.FORWARD_ONLY)
        logger.info(
            "Lifecycle.transition: scope=%s target=%s requested=%d matched=%d",
            scope,
            target.value,
            len(unit_ids),
            len(matches),
        )

    def supersede(self, scope: Scope, unit_id: str, invalid_at: datetime) -> MemoryUnit:
        units = self._storage.get(scope, [unit_id])
        for unit in units:
            _ensure_transition_allowed(unit.lifecycle, LifecycleState.SUPERSEDED, unit.id)
            unit.lifecycle = LifecycleState.SUPERSEDED
            unit.temporal.t_invalid = invalid_at
            self._storage.update(scope, [unit], mode=IndexWriteMode.FORWARD_ONLY)
            logger.info(
                "Lifecycle.supersede: unit_id=%s scope=%s invalid_at=%s",
                unit_id,
                scope,
                invalid_at,
            )
            return unit
        logger.warning("Lifecycle.supersede missing unit: unit_id=%s scope=%s", unit_id, scope)
        raise NotFoundError("memory_unit", unit_id)

    def sweep(self) -> list[str]:
        now = datetime.now(timezone.utc)
        swept: list[str] = []
        for scope in self._storage.scopes():
            units = self._storage.list(scope, limit=1_000_000).items
            for unit in units:
                target = _sweep_target(unit, now, self._policy)
                if target is not None:
                    unit.lifecycle = target
                    self._storage.update(scope, [unit], mode=IndexWriteMode.FORWARD_ONLY)
                    swept.append(unit.id)
        swept.sort()
        logger.info("Lifecycle.sweep: swept=%d", len(swept))
        return swept


# -- 注册到 LifecycleProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@LifecycleProducer.register("kv")
def _build(config):
    return KVLifecycleManager(
        StorageProducer.resolve(config),
        PolicyProducer.dep(config, default="dict"),
    )
