from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jiuwen_memory.api import DeleteMode, DeleteSelector, Scope
from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Segment, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps


def test_delete_selector_matches_tags_within_scope() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    stale = kernel.api.add(
        "old temporary note", scope, security=legacy_request_context(actor), tags=["temp"]
    )[0]
    keep = kernel.api.add(
        "fresh durable note", scope, security=legacy_request_context(actor), tags=["durable"]
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(scope=scope, tags=["temp"], mode=DeleteMode.ARCHIVE),
        security=legacy_request_context(actor),
    )

    assert stale.id in affected
    assert keep.id not in affected
    assert all(
        "temp" in kernel.api.get(unit_id, scope, security=legacy_request_context(actor)).tags
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=legacy_request_context(actor)).lifecycle
        == LifecycleState.ARCHIVED
        for unit_id in affected
    )
    assert (
        kernel.api.get(stale.id, scope, security=legacy_request_context(actor)).lifecycle
        == LifecycleState.ARCHIVED
    )
    assert (
        kernel.api.get(keep.id, scope, security=legacy_request_context(actor)).lifecycle
        == LifecycleState.ACTIVE
    )


def test_delete_selector_matches_before_event_time() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    old = kernel.api.add(
        "old event",
        scope,
        security=legacy_request_context(actor),
        occurred_at=datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc),
    )[0]
    new = kernel.api.add(
        "new event",
        scope,
        security=legacy_request_context(actor),
        occurred_at=datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc),
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(
            scope=scope,
            before=datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc),
            mode=DeleteMode.FORGET,
        ),
        security=legacy_request_context(actor),
    )

    cutoff = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    assert old.id in affected
    assert new.id not in affected
    assert all(
        kernel.api.get(unit_id, scope, security=legacy_request_context(actor)).temporal.t_message
        < cutoff
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=legacy_request_context(actor)).lifecycle
        == LifecycleState.FORGOTTEN
        for unit_id in affected
    )
    assert (
        kernel.api.get(old.id, scope, security=legacy_request_context(actor)).lifecycle
        == LifecycleState.FORGOTTEN
    )
    assert (
        kernel.api.get(new.id, scope, security=legacy_request_context(actor)).lifecycle
        == LifecycleState.ACTIVE
    )


def test_delete_selector_combines_conditions_with_and() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    matching = kernel.api.add(
        "old temp",
        scope,
        security=legacy_request_context(actor),
        tags=["temp"],
        occurred_at=datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc),
    )[0]
    wrong_tag = kernel.api.add(
        "old durable",
        scope,
        security=legacy_request_context(actor),
        tags=["durable"],
        occurred_at=datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc),
    )[0]
    too_new = kernel.api.add(
        "new temp",
        scope,
        security=legacy_request_context(actor),
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
        security=legacy_request_context(actor),
    )

    cutoff = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    assert matching.id in affected
    assert wrong_tag.id not in affected
    assert too_new.id not in affected
    assert all(
        "temp" in kernel.api.get(unit_id, scope, security=legacy_request_context(actor)).tags
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=legacy_request_context(actor)).temporal.t_message
        < cutoff
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=legacy_request_context(actor)).lifecycle
        == LifecycleState.FORGOTTEN
        for unit_id in affected
    )
    assert (
        kernel.api.get(matching.id, scope, security=legacy_request_context(actor)).lifecycle
        == LifecycleState.FORGOTTEN
    )
    assert (
        kernel.api.get(wrong_tag.id, scope, security=legacy_request_context(actor)).lifecycle
        == LifecycleState.ACTIVE
    )
    assert (
        kernel.api.get(too_new.id, scope, security=legacy_request_context(actor)).lifecycle
        == LifecycleState.ACTIVE
    )


def test_empty_delete_selector_raises_validation_error() -> None:
    kernel = build_kernel()

    with pytest.raises(ValidationError):
        kernel.api.delete(
            DeleteSelector(), security=legacy_request_context(Scope(org="acme", user="u1"))
        )


def test_delete_downweight_updates_importance_without_changing_lifecycle() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    unit = kernel.api.add(
        "lower priority",
        scope,
        security=legacy_request_context(actor),
        system_metadata={"importance": "0.8"},
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(unit_ids=[unit.id], scope=scope, mode=DeleteMode.DOWNWEIGHT),
        security=legacy_request_context(actor),
    )

    stored = kernel.api.get(unit.id, scope, security=legacy_request_context(actor))
    assert affected == [unit.id]
    assert stored.lifecycle == LifecycleState.ACTIVE
    assert stored.system_metadata["importance"] == "0.4"


def test_delete_purge_removes_memory_unit_from_truth_store() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    unit = kernel.api.add("remove permanently", scope, security=legacy_request_context(actor))[0]

    affected = kernel.api.delete(
        DeleteSelector(unit_ids=[unit.id], scope=scope, mode=DeleteMode.PURGE),
        security=legacy_request_context(actor),
    )

    assert unit.id in affected
    for unit_id in affected:
        with pytest.raises(NotFoundError):
            kernel.api.get(unit_id, scope, security=legacy_request_context(actor))


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
            security=legacy_request_context(actor),
        )

    assert (
        kernel.api.get(forgotten.id, scope, security=legacy_request_context(actor)).lifecycle
        == LifecycleState.FORGOTTEN
    )


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
        security=legacy_request_context(actor),
    )

    assert set(affected) == {source.id, direct.id, nested.id}
    for unit_id in [source.id, direct.id, nested.id]:
        with pytest.raises(NotFoundError):
            kernel.api.get(unit_id, scope, security=legacy_request_context(actor))
    assert (
        kernel.api.get(unrelated.id, scope, security=legacy_request_context(actor)).id
        == unrelated.id
    )
