# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~control.lifecycle.LifecycleManager`。

非破坏式状态流转：把记忆单元在真源里改 ``lifecycle``（active→superseded/archived/
forgotten），不物理删除。``sweep`` 是**纯计算**——扫描到期（``t_invalid`` 已过）
的 active 单元和 superseded 旧版本，返回待执行的
:class:`~control.lifecycle.SweepTransition`，不改真源、不触碰检索索引；索引清理与
真源回写由 Engine/Governance 编排执行（见 ``MemoryEngine.sweep_expired``）。

真源读写数据源**文档分流**：注入 :class:`~storage.domain_store.DomainStore` 后，
``transition``/``sweep`` 按 ``domain_store.should_write_document()`` 分两路——
文档模式（write_document=true）经 DomainStore 走 ``md``+``shadow`` 真源（
``get``/``list`` 读 + ``update`` 回写 lifecycle，content 不变 → shadow 只覆写
unit_json + lifecycle 投影列，不碰 md）；非文档模式维持经注入的
:class:`~storage.kv.KVStore` 端口（``scopes()`` 跨 scope 扫描 + ``load_units``/
``list_units`` 读 + ``memory_key``/``dumps`` 回写，同 ``ForwardIndexBuilder``
模式——回写即正排本体，无检索索引需要拆分）。``domain_store=None`` 时退化为纯 KV
实现（向后兼容直构与测试）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from jiuwen_memory.common.errors import NotFoundError, PolicyError, ValidationError
from jiuwen_memory.common.log import get_logger, scope_for_log
from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryUnit,
    Scope,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.lifecycle import (
    LifecycleManager,
    LifecycleProducer,
    SweepTransition,
)
from jiuwen_memory.control.policy import PolicyManager, PolicyProducer
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.kv import KVStore, list_units, load_units
from jiuwen_memory.storage.store_manager import StoreManagerProducer, resolve_name

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
    """在真源上做非破坏式状态流转与到期清扫（文档分流，见模块 docstring）。"""

    def __init__(
        self,
        kv: KVStore,
        policy: PolicyManager | None = None,
        domain_store: DomainStore | None = None,
    ) -> None:
        self._kv = kv
        self._policy = policy
        self._domain_store = domain_store

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def _write_units(self, scope: Scope, units: list[MemoryUnit]) -> None:
        """回写正排本体：文档模式经 DomainStore（content 不变 → shadow 只覆写
        lifecycle 投影列），非文档模式 key=memory_key、value=dumps 回写 KV。"""
        if self._domain_store is not None and self._domain_store.should_write_document():
            self._domain_store.update(scope, units)
            return
        for unit in units:
            self._kv.update(scope, memory_key(unit.id), dumps(unit))

    def transition(
        self, scope: Scope, unit_ids: list[str], target: LifecycleState
    ) -> None:
        # 文档模式：DomainStore.get 读（文档分流到 shadow），非文档：KV load_units。
        if self._domain_store is not None and self._domain_store.should_write_document():
            matches = self._domain_store.get(scope, unit_ids)
        else:
            matches = load_units(self._kv, scope, unit_ids)
        for unit in matches:
            _ensure_transition_allowed(unit.lifecycle, target, unit.id)
            unit.lifecycle = target
        if matches:
            self._write_units(scope, matches)
        logger.info(
            "Lifecycle.transition: scope=%s target=%s requested=%d matched=%d",
            scope_for_log(scope),
            target.value,
            len(unit_ids),
            len(matches),
        )

    def supersede(self, scope: Scope, unit_id: str, invalid_at: datetime) -> MemoryUnit:
        units = load_units(self._kv, scope, [unit_id])
        for unit in units:
            _ensure_transition_allowed(unit.lifecycle, LifecycleState.SUPERSEDED, unit.id)
            unit.lifecycle = LifecycleState.SUPERSEDED
            unit.temporal.t_invalid = invalid_at
            self._write_units(scope, [unit])
            logger.info(
                "Lifecycle.supersede: unit_id=%s scope=%s invalid_at=%s",
                unit_id,
                scope_for_log(scope),
                invalid_at,
            )
            return unit
        logger.warning(
            "Lifecycle.supersede missing unit: unit_id=%s scope=%s",
            unit_id,
            scope_for_log(scope),
        )
        raise NotFoundError("memory_unit", unit_id)

    def sweep(self) -> list[SweepTransition]:
        now = datetime.now(UTC)
        transitions: list[SweepTransition] = []
        # 文档模式：DomainStore.scopes() + DomainStore.list 读全量（文档分流到 shadow，
        # 扫描含 SUPERSEDED 旧版——list 不筛 lifecycle）；非文档：KV scopes + list_units。
        if self._domain_store is not None and self._domain_store.should_write_document():
            scope_units: list[tuple[Scope, list[MemoryUnit]]] = [
                (scope, self._domain_store.list(scope, limit=1_000_000).items)
                for scope in self._domain_store.scopes()
            ]
        else:
            scope_units = [
                (scope, list_units(self._kv, scope, limit=1_000_000)[0])
                for scope in self._kv.scopes()
            ]
        for scope, units in scope_units:
            for unit in units:
                target = _sweep_target(unit, now, self._policy)
                if target is None:
                    continue
                transitions.append(
                    SweepTransition(
                        scope=scope,
                        unit_id=unit.id,
                        from_state=unit.lifecycle,
                        to_state=target,
                        unit=unit,
                    )
                )
        transitions.sort(key=lambda t: t.unit_id)
        logger.info("Lifecycle.sweep: pending=%d", len(transitions))
        return transitions


# -- 注册到 LifecycleProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@LifecycleProducer.register("kv")
def _build(config):
    manager = StoreManagerProducer.resolve(config)
    return KVLifecycleManager(
        manager.kv(resolve_name(config, "kv_store")),
        PolicyProducer.dep(config, default="dict"),
        domain_store=manager.domain_store(resolve_name(config, "domain_store")),
    )
