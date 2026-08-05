from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api import DeleteMode, DeleteSelector, Scope
from api.memory_api_impl import build_kernel
from common.errors import NotFoundError, ValidationError
from common.type_def import LifecycleState, MemoryUnit, Segment, memory_key
from common.type_def.memory_codec import dumps
from tests.conftest import sec


def test_delete_selector_matches_tags_within_scope() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    stale = kernel.api.write("old temporary note", scope, security=sec(actor), tags=["temp"])[0]
    keep = kernel.api.write("fresh durable note", scope, security=sec(actor), tags=["durable"])[0]

    affected = kernel.api.delete(
        DeleteSelector(scope=scope, tags=["temp"], mode=DeleteMode.ARCHIVE),
        security=sec(actor),
    )

    assert stale.id in affected
    assert keep.id not in affected
    assert all(
        "temp" in kernel.api.get(unit_id, scope, security=sec(actor)).tags
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=sec(actor)).lifecycle == LifecycleState.ARCHIVED
        for unit_id in affected
    )
    assert kernel.api.get(stale.id, scope, security=sec(actor)).lifecycle == LifecycleState.ARCHIVED
    assert kernel.api.get(keep.id, scope, security=sec(actor)).lifecycle == LifecycleState.ACTIVE


def test_delete_selector_matches_before_event_time() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    old = kernel.api.write(
        "old event",
        scope,
        security=sec(actor),
        occurred_at=datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc),
    )[0]
    new = kernel.api.write(
        "new event",
        scope,
        security=sec(actor),
        occurred_at=datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc),
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(
            scope=scope,
            before=datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc),
            mode=DeleteMode.FORGET,
        ),
        security=sec(actor),
    )

    cutoff = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    assert old.id in affected
    assert new.id not in affected
    assert all(
        kernel.api.get(unit_id, scope, security=sec(actor)).temporal.t_event < cutoff
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=sec(actor)).lifecycle == LifecycleState.FORGOTTEN
        for unit_id in affected
    )
    assert kernel.api.get(old.id, scope, security=sec(actor)).lifecycle == LifecycleState.FORGOTTEN
    assert kernel.api.get(new.id, scope, security=sec(actor)).lifecycle == LifecycleState.ACTIVE


def test_delete_selector_combines_conditions_with_and() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    matching = kernel.api.write(
        "old temp",
        scope,
        security=sec(actor),
        tags=["temp"],
        occurred_at=datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc),
    )[0]
    wrong_tag = kernel.api.write(
        "old durable",
        scope,
        security=sec(actor),
        tags=["durable"],
        occurred_at=datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc),
    )[0]
    too_new = kernel.api.write(
        "new temp",
        scope,
        security=sec(actor),
        tags=["temp"],
        occurred_at=datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc),
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(
            scope=scope,
            tags=["temp"],
            before=datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc),
            mode=DeleteMode.FORGET,
        ),
        security=sec(actor),
    )

    cutoff = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    assert matching.id in affected
    assert wrong_tag.id not in affected
    assert too_new.id not in affected
    assert all(
        "temp" in kernel.api.get(unit_id, scope, security=sec(actor)).tags
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=sec(actor)).temporal.t_event < cutoff
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=sec(actor)).lifecycle == LifecycleState.FORGOTTEN
        for unit_id in affected
    )
    got = kernel.api.get(matching.id, scope, security=sec(actor))
    assert got.lifecycle == LifecycleState.FORGOTTEN
    kept = kernel.api.get(wrong_tag.id, scope, security=sec(actor))
    assert kept.lifecycle == LifecycleState.ACTIVE
    assert kernel.api.get(too_new.id, scope, security=sec(actor)).lifecycle == LifecycleState.ACTIVE


def test_empty_delete_selector_raises_validation_error() -> None:
    kernel = build_kernel()

    with pytest.raises(ValidationError):
        kernel.api.delete(DeleteSelector(), security=sec(Scope(org="acme", user="u1")))


def test_delete_downweight_updates_importance_without_changing_lifecycle() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    unit = kernel.api.write(
        "lower priority",
        scope,
        security=sec(actor),
        metadata={"importance": "0.8"},
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(unit_ids=[unit.id], scope=scope, mode=DeleteMode.DOWNWEIGHT),
        security=sec(actor),
    )

    stored = kernel.api.get(unit.id, scope, security=sec(actor))
    assert affected == [unit.id]
    assert stored.lifecycle == LifecycleState.ACTIVE
    assert stored.metadata["importance"] == "0.4"


def test_delete_purge_removes_memory_unit_from_truth_store() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    unit = kernel.api.write("remove permanently", scope, security=sec(actor))[0]

    affected = kernel.api.delete(
        DeleteSelector(unit_ids=[unit.id], scope=scope, mode=DeleteMode.PURGE),
        security=sec(actor),
    )

    assert unit.id in affected
    for unit_id in affected:
        with pytest.raises(NotFoundError):
            kernel.api.get(unit_id, scope, security=sec(actor))


def test_delete_archive_uses_lifecycle_transition_validation() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    forgotten = MemoryUnit(
        id="forgotten-unit",
        scope=scope,
        segments=[Segment(content="already forgotten")],
        lifecycle=LifecycleState.FORGOTTEN,
    )
    kernel.kv.insert(scope, memory_key(forgotten.id), dumps(forgotten))

    with pytest.raises(ValidationError):
        kernel.api.delete(
            DeleteSelector(unit_ids=[forgotten.id], scope=scope, mode=DeleteMode.ARCHIVE),
            security=sec(actor),
        )

    got = kernel.api.get(forgotten.id, scope, security=sec(actor))
    assert got.lifecycle == LifecycleState.FORGOTTEN


def test_delete_purge_recursively_removes_provenance_descendants() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    source = MemoryUnit(id="source", scope=scope, segments=[Segment(content="source")])
    direct = MemoryUnit(
        id="direct-derived",
        scope=scope,
        segments=[Segment(content="direct")],
        provenance=[source.id],
    )
    nested = MemoryUnit(
        id="nested-derived",
        scope=scope,
        segments=[Segment(content="nested")],
        provenance=[direct.id],
    )
    unrelated = MemoryUnit(id="unrelated", scope=scope, segments=[Segment(content="unrelated")])
    for unit in [source, direct, nested, unrelated]:
        kernel.kv.insert(scope, memory_key(unit.id), dumps(unit))

    affected = kernel.api.delete(
        DeleteSelector(unit_ids=[source.id], scope=scope, mode=DeleteMode.PURGE),
        security=sec(actor),
    )

    assert set(affected) == {source.id, direct.id, nested.id}
    for unit_id in [source.id, direct.id, nested.id]:
        with pytest.raises(NotFoundError):
            kernel.api.get(unit_id, scope, security=sec(actor))
    assert kernel.api.get(unrelated.id, scope, security=sec(actor)).id == unrelated.id
