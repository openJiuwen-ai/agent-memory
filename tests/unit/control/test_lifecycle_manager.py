from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api import Scope
from api.memory_api_impl import build_kernel
from common.errors import NotFoundError, PolicyError, ValidationError
from common.type_def import LifecycleState, memory_key
from common.type_def.memory_codec import dumps, loads
from control.lifecycle_impl.kv_lifecycle_manager import KVLifecycleManager
from control.policy_impl.dict_policy_manager import DictPolicyManager
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore


def _store(unit) -> tuple[InMemoryKVStore, KVLifecycleManager]:
    kv = InMemoryKVStore()
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    return kv, KVLifecycleManager(kv)


def _load(kv: InMemoryKVStore, unit) -> object:
    return loads(kv.get(unit.scope, memory_key(unit.id)))


def test_transition_allows_defined_forward_lifecycle_moves(unit_factory) -> None:
    unit = unit_factory("u1", "active memory", lifecycle=LifecycleState.ACTIVE)
    kv, lifecycle = _store(unit)

    lifecycle.transition([unit.id], LifecycleState.ARCHIVED)

    assert _load(kv, unit).lifecycle == LifecycleState.ARCHIVED


def test_transition_rejects_reactivating_forgotten_memory(unit_factory) -> None:
    unit = unit_factory("u1", "forgotten memory", lifecycle=LifecycleState.FORGOTTEN)
    kv, lifecycle = _store(unit)

    with pytest.raises(ValidationError):
        lifecycle.transition([unit.id], LifecycleState.ACTIVE)

    assert _load(kv, unit).lifecycle == LifecycleState.FORGOTTEN


def test_transition_rejects_reactivating_superseded_memory(unit_factory) -> None:
    unit = unit_factory("u1", "old version", lifecycle=LifecycleState.SUPERSEDED)
    kv, lifecycle = _store(unit)

    with pytest.raises(ValidationError):
        lifecycle.transition([unit.id], LifecycleState.ACTIVE)

    assert _load(kv, unit).lifecycle == LifecycleState.SUPERSEDED


def test_supersede_sets_state_and_invalid_time(unit_factory) -> None:
    invalid_at = datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc)
    unit = unit_factory("u1", "old version", lifecycle=LifecycleState.ACTIVE)
    kv, lifecycle = _store(unit)

    updated = lifecycle.supersede(unit.id, invalid_at)

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
        lifecycle.supersede(unit.id, invalid_at)

    stored = _load(kv, unit)
    assert stored.lifecycle == LifecycleState.ARCHIVED
    assert stored.temporal.t_invalid is None


def test_supersede_raises_not_found_for_missing_unit() -> None:
    lifecycle = KVLifecycleManager(InMemoryKVStore())

    with pytest.raises(NotFoundError):
        lifecycle.supersede("missing", datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc))


def test_sweep_forgets_expired_active_and_superseded_units(unit_factory) -> None:
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
    lifecycle = KVLifecycleManager(kv)

    swept = lifecycle.sweep()

    assert swept == ["expired", "superseded"]
    assert _load(kv, expired).lifecycle == LifecycleState.FORGOTTEN
    assert _load(kv, superseded).lifecycle == LifecycleState.FORGOTTEN
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
    lifecycle = KVLifecycleManager(kv, policy)

    assert lifecycle.sweep() == ["expired", "superseded"]
    assert _load(kv, expired).lifecycle == LifecycleState.ARCHIVED
    assert _load(kv, superseded).lifecycle == LifecycleState.ARCHIVED


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
    lifecycle = KVLifecycleManager(kv, policy)

    with pytest.raises(PolicyError):
        lifecycle.sweep()

    assert _load(kv, expired).lifecycle == LifecycleState.ACTIVE


def test_default_kernel_exposes_lifecycle_policy_keys() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    api = build_kernel().api

    assert api.admin_get("lifecycle.expired_active.target", identity=scope) == "forgotten"
    assert api.admin_get("lifecycle.superseded.target", identity=scope) == "forgotten"

    api.admin_set("lifecycle.expired_active.target", "archived", identity=scope)
    assert api.admin_get("lifecycle.expired_active.target", identity=scope) == "archived"


def test_default_kernel_lifecycle_sweep_uses_runtime_policy(unit_factory) -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    kernel = build_kernel()
    api = kernel.api
    expired = unit_factory(
        "expired-policy-smoke",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=datetime.now(timezone.utc) - timedelta(days=1),
    )
    kernel.kv.insert(scope, memory_key(expired.id), dumps(expired))

    api.admin_set("lifecycle.expired_active.target", "archived", identity=scope)
    swept = getattr(getattr(api, "_engine"), "_lifecycle").sweep()

    stored = loads(kernel.kv.get(scope, memory_key(expired.id)))
    assert swept == [expired.id]
    assert stored.lifecycle == LifecycleState.ARCHIVED
