from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jiuwen_memory.api import DeleteMode, DeleteSelector, Scope
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Segment, Temporal, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.control.engine_impl.temporal_support import matches_delete_selector
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from tests.support.scoped_authenticator import ScopedAuthenticator
from tests.unit.api.fixtures import security_for


def test_delete_selector_matches_tags_within_scope() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    stale = kernel.api.add(
        "old temporary note",
        scope,
        security=security_for(kernel.api, actor),
        tags=["temp"],
    )[0]
    keep = kernel.api.add(
        "fresh durable note",
        scope,
        security=security_for(kernel.api, actor),
        tags=["durable"],
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(scope=scope, tags=["temp"], mode=DeleteMode.ARCHIVE),
        security=security_for(kernel.api, actor),
    )

    assert stale.id in affected
    assert keep.id not in affected
    assert all(
        "temp" in kernel.api.get(unit_id, scope, security=security_for(kernel.api, actor)).tags
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=security_for(kernel.api, actor)).lifecycle
        == LifecycleState.ARCHIVED
        for unit_id in affected
    )
    assert (
        kernel.api.get(stale.id, scope, security=security_for(kernel.api, actor)).lifecycle
        == LifecycleState.ARCHIVED
    )
    assert (
        kernel.api.get(keep.id, scope, security=security_for(kernel.api, actor)).lifecycle
        == LifecycleState.ACTIVE
    )


def test_delete_selector_matches_before_event_time() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    old = kernel.api.add(
        "old event",
        scope,
        security=security_for(kernel.api, actor),
        occurred_at=datetime(2026, 6, 17, 9, 0, tzinfo=UTC),
    )[0]
    new = kernel.api.add(
        "new event",
        scope,
        security=security_for(kernel.api, actor),
        occurred_at=datetime(2026, 6, 17, 12, 0, tzinfo=UTC),
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(
            scope=scope,
            before=datetime(2026, 6, 17, 10, 0, tzinfo=UTC),
            mode=DeleteMode.FORGET,
        ),
        security=security_for(kernel.api, actor),
    )

    cutoff = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)
    assert old.id in affected
    assert new.id not in affected
    assert all(
        kernel.api.get(unit_id, scope, security=security_for(kernel.api, actor)).temporal.t_message
        < cutoff
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=security_for(kernel.api, actor)).lifecycle
        == LifecycleState.FORGOTTEN
        for unit_id in affected
    )
    assert (
        kernel.api.get(old.id, scope, security=security_for(kernel.api, actor)).lifecycle
        == LifecycleState.FORGOTTEN
    )
    assert (
        kernel.api.get(new.id, scope, security=security_for(kernel.api, actor)).lifecycle
        == LifecycleState.ACTIVE
    )


def test_delete_selector_combines_conditions_with_and() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    matching = kernel.api.add(
        "old temp",
        scope,
        security=security_for(kernel.api, actor),
        tags=["temp"],
        occurred_at=datetime(2026, 6, 17, 9, 0, tzinfo=UTC),
    )[0]
    wrong_tag = kernel.api.add(
        "old durable",
        scope,
        security=security_for(kernel.api, actor),
        tags=["durable"],
        occurred_at=datetime(2026, 6, 17, 9, 0, tzinfo=UTC),
    )[0]
    too_new = kernel.api.add(
        "new temp",
        scope,
        security=security_for(kernel.api, actor),
        tags=["temp"],
        occurred_at=datetime(2026, 6, 17, 12, 0, tzinfo=UTC),
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(
            scope=scope,
            tags=["temp"],
            before=datetime(2026, 6, 17, 10, 0, tzinfo=UTC),
            mode=DeleteMode.FORGET,
        ),
        security=security_for(kernel.api, actor),
    )

    cutoff = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)
    assert matching.id in affected
    assert wrong_tag.id not in affected
    assert too_new.id not in affected
    assert all(
        "temp" in kernel.api.get(unit_id, scope, security=security_for(kernel.api, actor)).tags
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=security_for(kernel.api, actor)).temporal.t_message
        < cutoff
        for unit_id in affected
    )
    assert all(
        kernel.api.get(unit_id, scope, security=security_for(kernel.api, actor)).lifecycle
        == LifecycleState.FORGOTTEN
        for unit_id in affected
    )
    assert (
        kernel.api.get(matching.id, scope, security=security_for(kernel.api, actor)).lifecycle
        == LifecycleState.FORGOTTEN
    )
    assert (
        kernel.api.get(wrong_tag.id, scope, security=security_for(kernel.api, actor)).lifecycle
        == LifecycleState.ACTIVE
    )
    assert (
        kernel.api.get(too_new.id, scope, security=security_for(kernel.api, actor)).lifecycle
        == LifecycleState.ACTIVE
    )


def test_empty_delete_selector_raises_validation_error() -> None:
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)

    with pytest.raises(ValidationError):
        kernel.api.delete(
            DeleteSelector(),
            security=internal_context(ScopedAuthenticator(Scope(org="acme", user="u1"))),
        )


def test_delete_downweight_updates_importance_without_changing_lifecycle() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    unit = kernel.api.add(
        "lower priority",
        scope,
        security=security_for(kernel.api, actor),
        system_metadata={"importance": "0.8"},
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(unit_ids=[unit.id], scope=scope, mode=DeleteMode.DOWNWEIGHT),
        security=security_for(kernel.api, actor),
    )

    stored = kernel.api.get(unit.id, scope, security=security_for(kernel.api, actor))
    assert affected == [unit.id]
    assert stored.lifecycle == LifecycleState.ACTIVE
    assert stored.system_metadata["importance"] == "0.4"


def test_delete_purge_removes_memory_unit_from_truth_store() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    unit = kernel.api.add("remove permanently", scope, security=security_for(kernel.api, actor))[0]

    affected = kernel.api.delete(
        DeleteSelector(unit_ids=[unit.id], scope=scope, mode=DeleteMode.PURGE),
        security=security_for(kernel.api, actor),
    )

    assert unit.id in affected
    for unit_id in affected:
        with pytest.raises(NotFoundError):
            kernel.api.get(unit_id, scope, security=security_for(kernel.api, actor))


def test_delete_archive_uses_lifecycle_transition_validation() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    forgotten = MemoryUnit(
        id="forgotten-unit",
        scope=scope,
        segments=[Segment(content="already forgotten")],
        lifecycle=LifecycleState.FORGOTTEN,
    )
    kv.insert(scope, memory_key(forgotten.id), dumps(forgotten))

    with pytest.raises(ValidationError):
        kernel.api.delete(
            DeleteSelector(unit_ids=[forgotten.id], scope=scope, mode=DeleteMode.ARCHIVE),
            security=security_for(kernel.api, actor),
        )

    assert (
        kernel.api.get(forgotten.id, scope, security=security_for(kernel.api, actor)).lifecycle
        == LifecycleState.FORGOTTEN
    )


def test_delete_purge_recursively_removes_provenance_descendants() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
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
        kv.insert(scope, memory_key(unit.id), dumps(unit))

    affected = kernel.api.delete(
        DeleteSelector(unit_ids=[source.id], scope=scope, mode=DeleteMode.PURGE),
        security=security_for(kernel.api, actor),
    )

    assert set(affected) == {source.id, direct.id, nested.id}
    for unit_id in [source.id, direct.id, nested.id]:
        with pytest.raises(NotFoundError):
            kernel.api.get(unit_id, scope, security=security_for(kernel.api, actor))
    assert (
        kernel.api.get(unrelated.id, scope, security=security_for(kernel.api, actor)).id
        == unrelated.id
    )


def test_delete_selector_before_matches_naive_message_time_with_aware_cutoff() -> None:
    """契约层两种合法 ISO 形态组合：naive occurred_at 写入 + aware before 删除。"""
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    old = kernel.api.add(
        "old event",
        scope,
        security=security_for(kernel.api, actor),
        occurred_at=datetime(2026, 6, 17, 9, 0),  # naive
    )[0]
    new = kernel.api.add(
        "new event",
        scope,
        security=security_for(kernel.api, actor),
        occurred_at=datetime(2026, 6, 17, 12, 0),  # naive
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(
            scope=scope,
            before=datetime(2026, 6, 17, 10, 0, tzinfo=UTC),  # aware
            mode=DeleteMode.FORGET,
        ),
        security=security_for(kernel.api, actor),
    )

    assert old.id in affected, "naive t_message 早于 aware before 应命中，不得因混比抛 TypeError"
    assert new.id not in affected


def test_delete_selector_before_matches_aware_message_time_with_naive_cutoff() -> None:
    """反向组合：aware occurred_at 写入 + naive before 删除。"""
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    old = kernel.api.add(
        "old event",
        scope,
        security=security_for(kernel.api, actor),
        occurred_at=datetime(2026, 6, 17, 9, 0, tzinfo=UTC),
    )[0]
    new = kernel.api.add(
        "new event",
        scope,
        security=security_for(kernel.api, actor),
        occurred_at=datetime(2026, 6, 17, 12, 0, tzinfo=UTC),
    )[0]

    affected = kernel.api.delete(
        DeleteSelector(
            scope=scope,
            before=datetime(2026, 6, 17, 10, 0),  # naive
            mode=DeleteMode.FORGET,
        ),
        security=security_for(kernel.api, actor),
    )

    assert old.id in affected, "aware t_message 早于 naive before 应命中，不得因混比抛 TypeError"
    assert new.id not in affected


SCOPE = Scope(org="acme", user="u1", agent="a1", session="s1")

# 同一时刻的两种合法 ISO 形态：naive 按 UTC 解释，aware 带 +00:00。
NAIVE_9 = datetime(2026, 6, 17, 9, 0)
AWARE_9 = datetime(2026, 6, 17, 9, 0, tzinfo=UTC)
NAIVE_10 = datetime(2026, 6, 17, 10, 0)
AWARE_10 = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)



def _unit(t_message: datetime | None) -> MemoryUnit:
    return MemoryUnit(
        id="u1",
        scope=SCOPE,
        segments=[Segment(content="note")],
        temporal=Temporal(t_message=t_message),
    )


@pytest.mark.parametrize(
    ("t_message", "before", "expected"),
    [
        (NAIVE_9, AWARE_10, True),  # 契约层两种合法形态组合：先前必抛 TypeError
        (AWARE_9, NAIVE_10, True),
        (NAIVE_9, NAIVE_10, True),
        (AWARE_9, AWARE_10, True),
        (NAIVE_9, AWARE_9, False),  # 边界：t_message == before 不命中
        (AWARE_9, NAIVE_9, False),
    ],
)
def test_matches_delete_selector_compares_naive_and_aware_forms(
    t_message: datetime, before: datetime, expected: bool
) -> None:
    selector = DeleteSelector(unit_ids=["u1"], before=before, mode=DeleteMode.FORGET)

    assert matches_delete_selector(_unit(t_message), selector) is expected, (
        "before 与 t_message 的 naive/aware 组合必须按 UTC 归一后比较"
    )


def test_matches_delete_selector_skips_unit_without_message_time() -> None:
    selector = DeleteSelector(unit_ids=["u1"], before=AWARE_10)

    assert matches_delete_selector(_unit(None), selector) is False, "无 t_message 不命中 before"
