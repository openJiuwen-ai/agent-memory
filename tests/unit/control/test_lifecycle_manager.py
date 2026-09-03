from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import NotFoundError, PolicyError, ValidationError
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import LifecycleState, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.config.config import Config
from jiuwen_memory.control.lifecycle_impl.kv_lifecycle_manager import KVLifecycleManager
from jiuwen_memory.control.policy_impl.dict_policy_manager import DictPolicyManager
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage

pytestmark = pytest.mark.unit

_TEST_KEY_HEX = "00" * 32


def _store(unit) -> tuple[InMemoryKVStore, KVLifecycleManager]:
    kv = InMemoryKVStore()
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    return kv, KVLifecycleManager(CompositeStorage(kv=kv))


def _load(kv: InMemoryKVStore, unit) -> object:
    return loads(kv.get(unit.scope, memory_key(unit.id)))


def test_transition_allows_defined_forward_lifecycle_moves(unit_factory) -> None:
    unit = unit_factory("u1", "active memory", lifecycle=LifecycleState.ACTIVE)
    kv, lifecycle = _store(unit)

    lifecycle.transition(unit.scope, [unit.id], LifecycleState.ARCHIVED)

    assert _load(kv, unit).lifecycle == LifecycleState.ARCHIVED


def test_transition_rejects_reactivating_forgotten_memory(unit_factory) -> None:
    unit = unit_factory("u1", "forgotten memory", lifecycle=LifecycleState.FORGOTTEN)
    kv, lifecycle = _store(unit)

    with pytest.raises(ValidationError):
        lifecycle.transition(unit.scope, [unit.id], LifecycleState.ACTIVE)

    assert _load(kv, unit).lifecycle == LifecycleState.FORGOTTEN


def test_transition_rejects_reactivating_superseded_memory(unit_factory) -> None:
    unit = unit_factory("u1", "old version", lifecycle=LifecycleState.SUPERSEDED)
    kv, lifecycle = _store(unit)

    with pytest.raises(ValidationError):
        lifecycle.transition(unit.scope, [unit.id], LifecycleState.ACTIVE)

    assert _load(kv, unit).lifecycle == LifecycleState.SUPERSEDED


def test_supersede_sets_state_and_invalid_time(unit_factory) -> None:
    invalid_at = datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc)
    unit = unit_factory("u1", "old version", lifecycle=LifecycleState.ACTIVE)
    kv, lifecycle = _store(unit)

    updated = lifecycle.supersede(unit.scope, unit.id, invalid_at)

    stored = _load(kv, unit)
    assert updated.id == unit.id
    assert updated.lifecycle == LifecycleState.SUPERSEDED
    assert updated.temporal.t_invalid == invalid_at
    assert stored.lifecycle == LifecycleState.SUPERSEDED
    assert stored.temporal.t_invalid == invalid_at


def test_supersede_rejects_invalid_lifecycle_state(unit_factory) -> None:
    invalid_at = datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc)
    unit = unit_factory("u1", "archived version", lifecycle=LifecycleState.ARCHIVED)
    kv, lifecycle = _store(unit)

    with pytest.raises(ValidationError):
        lifecycle.supersede(unit.scope, unit.id, invalid_at)

    stored = _load(kv, unit)
    assert stored.lifecycle == LifecycleState.ARCHIVED
    assert stored.temporal.t_invalid is None


def test_supersede_raises_not_found_for_missing_unit() -> None:
    lifecycle = KVLifecycleManager(CompositeStorage(kv=InMemoryKVStore()))

    with pytest.raises(NotFoundError):
        lifecycle.supersede(
            Scope(),
            "missing",
            datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc),
        )


def test_targeted_transition_does_not_mutate_same_id_in_another_scope(unit_factory) -> None:
    scope_a = Scope(org="acme", space="space-a", user="alice")
    scope_b = Scope(org="acme", space="space-b", user="alice")
    unit_a = unit_factory("shared-id", "space A", lifecycle=LifecycleState.ACTIVE)
    unit_b = unit_factory("shared-id", "space B", lifecycle=LifecycleState.ACTIVE)
    unit_a.scope = scope_a
    unit_b.scope = scope_b
    kv = InMemoryKVStore()
    kv.insert(scope_a, memory_key(unit_a.id), dumps(unit_a))
    kv.insert(scope_b, memory_key(unit_b.id), dumps(unit_b))
    lifecycle = KVLifecycleManager(CompositeStorage(kv=kv))

    lifecycle.transition(scope_b, [unit_b.id], LifecycleState.FORGOTTEN)

    assert _load(kv, unit_a).lifecycle == LifecycleState.ACTIVE
    assert _load(kv, unit_b).lifecycle == LifecycleState.FORGOTTEN


def test_sweep_returns_pending_transitions_without_mutating_units(unit_factory) -> None:
    now = datetime.now(timezone.utc)
    expired = unit_factory(
        "expired",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=now - timedelta(days=1),
    )
    superseded = unit_factory("superseded", "old version", lifecycle=LifecycleState.SUPERSEDED)
    active = unit_factory(
        "active",
        "still active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=now + timedelta(days=1),
    )
    archived = unit_factory("archived", "archived memory", lifecycle=LifecycleState.ARCHIVED)
    forgotten = unit_factory("forgotten", "forgotten memory", lifecycle=LifecycleState.FORGOTTEN)
    kv = InMemoryKVStore()
    for unit in [expired, superseded, active, archived, forgotten]:
        kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    lifecycle = KVLifecycleManager(CompositeStorage(kv=kv))

    transitions = lifecycle.sweep()

    # 纯计算：只返回到期/旧版的待执行 transition，不改真源（回写由 Engine 编排）。
    assert [t.unit_id for t in transitions] == ["expired", "superseded"]
    assert all(t.to_state == LifecycleState.FORGOTTEN for t in transitions)
    assert all(
        t.from_state in (LifecycleState.ACTIVE, LifecycleState.SUPERSEDED) for t in transitions
    )
    assert all(t.unit.id == t.unit_id for t in transitions)
    assert _load(kv, expired).lifecycle == LifecycleState.ACTIVE
    assert _load(kv, superseded).lifecycle == LifecycleState.SUPERSEDED
    assert _load(kv, active).lifecycle == LifecycleState.ACTIVE
    assert _load(kv, archived).lifecycle == LifecycleState.ARCHIVED
    assert _load(kv, forgotten).lifecycle == LifecycleState.FORGOTTEN


def test_sweep_returns_empty_list_when_no_units_are_changed(unit_factory) -> None:
    unit = unit_factory("u1", "still active", lifecycle=LifecycleState.ACTIVE)
    kv, lifecycle = _store(unit)

    assert lifecycle.sweep() == []
    assert _load(kv, unit).lifecycle == LifecycleState.ACTIVE


def test_sweep_uses_policy_targets_for_expired_active_and_superseded(unit_factory) -> None:
    now = datetime.now(timezone.utc)
    expired = unit_factory(
        "expired",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=now - timedelta(days=1),
    )
    superseded = unit_factory("superseded", "old version", lifecycle=LifecycleState.SUPERSEDED)
    kv = InMemoryKVStore()
    for unit in [expired, superseded]:
        kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    policy = DictPolicyManager(
        {
            "lifecycle.expired_active.target": "archived",
            "lifecycle.superseded.target": "archived",
        }
    )
    lifecycle = KVLifecycleManager(CompositeStorage(kv=kv), policy)

    transitions = lifecycle.sweep()

    assert [t.unit_id for t in transitions] == ["expired", "superseded"]
    assert all(t.to_state == LifecycleState.ARCHIVED for t in transitions)
    # 纯计算：目标态来自策略，但真源回写由 Engine 编排执行。
    assert _load(kv, expired).lifecycle == LifecycleState.ACTIVE
    assert _load(kv, superseded).lifecycle == LifecycleState.SUPERSEDED


def test_sweep_rejects_invalid_policy_target(unit_factory) -> None:
    now = datetime.now(timezone.utc)
    expired = unit_factory(
        "expired",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=now - timedelta(days=1),
    )
    kv = InMemoryKVStore()
    kv.insert(expired.scope, memory_key(expired.id), dumps(expired))
    policy = DictPolicyManager(
        {
            "lifecycle.expired_active.target": "active",
            "lifecycle.superseded.target": "forgotten",
        }
    )
    lifecycle = KVLifecycleManager(CompositeStorage(kv=kv), policy)

    with pytest.raises(PolicyError):
        lifecycle.sweep()

    assert _load(kv, expired).lifecycle == LifecycleState.ACTIVE


def test_default_kernel_exposes_lifecycle_policy_keys() -> None:
    api = build_kernel().api
    root = Scope()

    assert (
        api.admin_get("lifecycle.expired_active.target", security=legacy_request_context(root))
        == "forgotten"
    )
    assert (
        api.admin_get("lifecycle.superseded.target", security=legacy_request_context(root))
        == "forgotten"
    )

    api.admin_set(
        "lifecycle.expired_active.target", "archived", security=legacy_request_context(root)
    )
    assert (
        api.admin_get("lifecycle.expired_active.target", security=legacy_request_context(root))
        == "archived"
    )


def test_default_kernel_lifecycle_sweep_uses_runtime_policy(unit_factory) -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    root = Scope()
    kv = InMemoryKVStore()
    kernel = build_kernel(
        kv=kv,
        config=Config.from_dict(
            {
                "security": {
                    "default": {"target": "local", "params": {"key_hex": _TEST_KEY_HEX}}
                }
            }
        )
    )
    api = kernel.api
    expired = unit_factory(
        "expired-policy-smoke",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=datetime.now(timezone.utc) - timedelta(days=1),
    )
    kv.insert(scope, memory_key(expired.id), dumps(expired))

    api.admin_set(
        "lifecycle.expired_active.target", "archived", security=legacy_request_context(root)
    )
    result = asyncio.run(api._engine.sweep_expired())

    stored = loads(kernel.kv.get(scope, memory_key(expired.id)))
    assert result.swept == [expired.id]
    assert result.failed == []
    assert stored.lifecycle == LifecycleState.ARCHIVED
