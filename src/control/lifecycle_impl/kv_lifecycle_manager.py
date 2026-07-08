"""最小实现：:class:`~control.lifecycle.LifecycleManager`。

非破坏式状态流转：把记忆单元在真源里改 ``lifecycle``（active→superseded/archived/
forgotten），不物理删除。``sweep`` 扫描到期（``t_invalid`` 已过）的 active 单元
和 superseded 旧版本，标记 FORGOTTEN，返回被处理的 id。真源读注入的
:class:`~storage.kv.KVStore`
（``scopes()`` + ``list()`` 跨 scope 扫描，字节经 memory_codec 编解码）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from common.errors import NotFoundError, PolicyError, ValidationError
from common.log import get_logger
from common.type_def import (
    MEMORY_KEY_PREFIX,
    LifecycleState,
    MemoryUnit,
    Scope,
    memory_key,
)
from common.type_def.memory_codec import dumps, loads
from control.base import ControlOperatorType
from control.lifecycle import LifecycleManager, LifecycleProducer
from control.policy import PolicyManager, PolicyProducer
from storage.kv import KvProducer, KVStore

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

    def __init__(self, kv: KVStore, policy: PolicyManager | None = None) -> None:
        self._kv = kv
        self._policy = policy

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def transition(self, unit_ids: List[str], target: LifecycleState) -> None:
        wanted = {memory_key(unit_id) for unit_id in unit_ids}
        matches: list[tuple[Scope, str, MemoryUnit]] = []
        for scope in self._kv.scopes():
            for key, raw in self._kv.list(scope, MEMORY_KEY_PREFIX):
                unit = loads(raw)
                if unit is None or key not in wanted:
                    continue
                _ensure_transition_allowed(unit.lifecycle, target, key)
                matches.append((scope, key, unit))
        for scope, key, unit in matches:
            unit.lifecycle = target
            self._kv.update(scope, key, dumps(unit))
        logger.info(
            "Lifecycle.transition: target=%s requested=%d matched=%d",
            target.value,
            len(wanted),
            len(matches),
        )

    def supersede(self, unit_id: str, invalid_at: datetime) -> MemoryUnit:
        dst_key = memory_key(unit_id)
        for scope in self._kv.scopes():
            for key, raw in self._kv.list(scope, MEMORY_KEY_PREFIX):
                if key == dst_key:
                    unit = loads(raw)
                    if unit is None:
                        continue
                    _ensure_transition_allowed(unit.lifecycle, LifecycleState.SUPERSEDED, key)
                    unit.lifecycle = LifecycleState.SUPERSEDED
                    unit.temporal.t_invalid = invalid_at
                    self._kv.update(scope, key, dumps(unit))
                    logger.info(
                        "Lifecycle.supersede: unit_id=%s scope=%s invalid_at=%s",
                        unit_id,
                        scope,
                        invalid_at,
                    )
                    return unit
        logger.warning("Lifecycle.supersede missing unit: unit_id=%s", unit_id)
        raise NotFoundError("memory_unit", unit_id)

    def sweep(self) -> List[str]:
        now = datetime.now(timezone.utc)
        swept: List[str] = []
        for scope in self._kv.scopes():
            for key, raw in self._kv.list(scope, MEMORY_KEY_PREFIX):
                unit = loads(raw)
                if unit is None:
                    continue
                target = _sweep_target(unit, now, self._policy)
                if target is not None:
                    unit.lifecycle = target
                    self._kv.update(scope, key, dumps(unit))
                    swept.append(unit.id)
        logger.info("Lifecycle.sweep: swept=%d", len(swept))
        return swept


# -- 注册到 LifecycleProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@LifecycleProducer.register("kv")
def _build(config):
    return KVLifecycleManager(
        KvProducer.dep(config, default="memory"),
        PolicyProducer.dep(config, default="dict"),
    )
