from __future__ import annotations

from datetime import datetime, timezone

from jiuwen_memory.api import MemoryPatch, Scope
from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.lifecycle import LifecycleManager


class RecordingLifecycle(LifecycleManager):
    def __init__(self, kv) -> None:
        self._kv = kv
        self.supersede_calls: list[tuple[Scope, str, datetime]] = []

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def transition(
        self, scope: Scope, unit_ids: list[str], target: LifecycleState
    ) -> None:
        raise AssertionError("Engine.update(SUPERSEDE) should call supersede(), not transition()")

    def supersede(self, scope: Scope, unit_id: str, invalid_at: datetime) -> MemoryUnit:
        self.supersede_calls.append((scope, unit_id, invalid_at))
        for key, raw in self._kv.scan(scope):
            if key.rsplit("/", 1)[-1] == unit_id:
                unit = loads(raw)
                if unit.lifecycle != LifecycleState.ACTIVE:
                    raise ValidationError("invalid test transition")
                unit.lifecycle = LifecycleState.SUPERSEDED
                unit.temporal.t_invalid = invalid_at
                self._kv.update(scope, key, dumps(unit))
                return unit
        raise AssertionError(f"missing test unit: {unit_id}")

    def sweep(self) -> list[str]:
        return []


def test_supersede_sets_version_chain_and_invalidates_old_version() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()

    old = kernel.api.add("home is Shanghai", scope, security=legacy_request_context(actor))[0]

    new = kernel.api.update(
        old.id,
        scope,
        MemoryPatch(content="home is Beijing"),
        security=legacy_request_context(actor),
    )

    stored_old = kernel.api.get(old.id, scope, security=legacy_request_context(actor))
    assert new.id != old.id
    assert new.supersedes == old.id
    assert new.temporal.t_valid is not None
    assert stored_old.lifecycle == LifecycleState.SUPERSEDED
    assert stored_old.temporal.t_invalid == new.temporal.t_valid


def test_supersede_uses_patch_valid_time_as_new_version_boundary() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    valid_from = datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc)

    old = kernel.api.add("home is Shanghai", scope, security=legacy_request_context(actor))[0]
    new = kernel.api.update(
        old.id,
        scope,
        MemoryPatch(content="home is Beijing", t_valid=valid_from),
        security=legacy_request_context(actor),
    )

    stored_old = kernel.api.get(old.id, scope, security=legacy_request_context(actor))
    assert new.temporal.t_valid == valid_from
    assert stored_old.temporal.t_invalid == valid_from


def test_update_supersede_delegates_old_version_lifecycle_to_manager() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    lifecycle = RecordingLifecycle(kernel.kv)
    setattr(getattr(kernel.api, "_engine"), "_lifecycle", lifecycle)
    valid_from = datetime(2026, 6, 17, 11, 0, tzinfo=timezone.utc)

    old = kernel.api.add("home is Shanghai", scope, security=legacy_request_context(actor))[0]
    new = kernel.api.update(
        old.id,
        scope,
        MemoryPatch(content="home is Beijing", t_valid=valid_from),
        security=legacy_request_context(actor),
    )

    assert lifecycle.supersede_calls == [(scope, old.id, valid_from)]
    stored_old = kernel.api.get(old.id, scope, security=legacy_request_context(actor))
    assert stored_old.lifecycle == LifecycleState.SUPERSEDED
    assert stored_old.temporal.t_invalid == valid_from
    assert new.supersedes == old.id
