from __future__ import annotations

from datetime import datetime, timezone

import pytest

from common.audit.audit_impl.in_memory_audit_logger import InMemoryAuditLogger
from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
from common.type_def import AuditEvent, Scope

pytestmark = pytest.mark.unit


def _logger(tmp_path, backend: str):
    return (
        InMemoryAuditLogger()
        if backend == "memory"
        else SqliteAuditLogger(str(tmp_path / "audit.sqlite3"))
    )


def _event(
    event_id: str,
    *,
    actor: Scope,
    action: str = "write",
    layer: str = "api",
    decision: str = "allow",
    target_id: str = "unit-1",
    target: Scope | None = None,
    occurred_at: datetime,
) -> AuditEvent:
    return AuditEvent(
        id=event_id,
        actor=actor,
        target=target or Scope(),
        action=action,
        target_id=target_id,
        layer=layer,
        decision=decision,
        occurred_at=occurred_at,
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize(
    ("field", "expected", "mismatch"),
    [
        ("action", "write", "delete"),
        ("layer", "api", "control"),
        ("decision", "allow", "deny"),
        ("target_id", "unit-1", "unit-2"),
        ("actor_org", "acme", "other"),
        ("actor_space", "product", "coding"),
        ("actor_user", "alice", "bob"),
        ("actor_agent", "agent-a", "agent-b"),
        ("actor_session", "s1", "s2"),
        ("target_org", "acme", "other"),
        ("target_space", "product", "coding"),
        ("target_user", "alice", "bob"),
        ("target_agent", "agent-a", "agent-b"),
        ("target_session", "s1", "s2"),
    ],
)
def test_audit_query_filters_each_exact_match_field(
    tmp_path,
    backend: str,
    field: str,
    expected: str,
    mismatch: str,
) -> None:
    logger = _logger(tmp_path, backend)
    occurred_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    matching_actor = Scope(
        org="acme",
        space="product",
        user="alice",
        agent="agent-a",
        session="s1",
    )
    matching_target = Scope(
        org="acme",
        space="product",
        user="alice",
        agent="agent-a",
        session="s1",
    )
    wrong_actor = matching_actor
    wrong_target = matching_target
    event_kwargs = {
        "action": "write",
        "layer": "api",
        "decision": "allow",
        "target_id": "unit-1",
    }

    if field in event_kwargs:
        event_kwargs[field] = mismatch
    elif field.startswith("actor_"):
        actor_values = {
            "actor_org": "acme",
            "actor_space": "product",
            "actor_user": "alice",
            "actor_agent": "agent-a",
            "actor_session": "s1",
        }
        actor_values[field] = mismatch
        wrong_actor = Scope(
            org=actor_values["actor_org"],
            space=actor_values["actor_space"],
            user=actor_values["actor_user"],
            agent=actor_values["actor_agent"],
            session=actor_values["actor_session"],
        )
    elif field.startswith("target_"):
        target_values = {
            "target_org": "acme",
            "target_space": "product",
            "target_user": "alice",
            "target_agent": "agent-a",
            "target_session": "s1",
        }
        target_values[field] = mismatch
        wrong_target = Scope(
            org=target_values["target_org"],
            space=target_values["target_space"],
            user=target_values["target_user"],
            agent=target_values["target_agent"],
            session=target_values["target_session"],
        )

    logger.record(
        _event("match", actor=matching_actor, target=matching_target, occurred_at=occurred_at)
    )
    logger.record(
        _event(
            "wrong",
            actor=wrong_actor,
            target=wrong_target,
            occurred_at=occurred_at,
            **event_kwargs,
        )
    )

    events = logger.query({field: expected})

    assert [event.id for event in events] == ["match"], f"{field} should filter exactly"


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_audit_query_filters_can_combine_structured_fields(tmp_path, backend: str) -> None:
    logger = _logger(tmp_path, backend)
    matching_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    too_old = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    too_new = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    actor = Scope(org="acme", space="product", user="alice", agent="agent-a", session="s1")
    target = Scope(org="acme", space="product", user="alice", agent="agent-a", session="s1")

    logger.record(_event("match", actor=actor, target=target, occurred_at=matching_time))
    logger.record(
        _event(
            "wrong-decision",
            actor=actor,
            target=target,
            decision="deny",
            occurred_at=matching_time,
        )
    )
    logger.record(
        _event(
            "wrong-target",
            actor=actor,
            target=target,
            target_id="unit-2",
            occurred_at=matching_time,
        )
    )
    logger.record(
        _event(
            "wrong-actor",
            actor=Scope(org="acme", user="bob"),
            target=target,
            occurred_at=matching_time,
        )
    )
    logger.record(
        _event(
            "wrong-target-scope",
            actor=actor,
            target=Scope(org="acme", space="other"),
            occurred_at=matching_time,
        )
    )
    logger.record(_event("too-old", actor=actor, target=target, occurred_at=too_old))
    logger.record(_event("too-new", actor=actor, target=target, occurred_at=too_new))

    events = logger.query(
        {
            "action": "write",
            "layer": "api",
            "decision": "allow",
            "target_id": "unit-1",
            "actor_org": "acme",
            "actor_space": "product",
            "actor_user": "alice",
            "actor_agent": "agent-a",
            "actor_session": "s1",
            "target_org": "acme",
            "target_space": "product",
            "target_user": "alice",
            "target_agent": "agent-a",
            "target_session": "s1",
            "occurred_after": "2026-01-01T11:00:00+00:00",
            "occurred_before": "2026-01-01T13:00:00+00:00",
        }
    )

    assert [event.id for event in events] == ["match"]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_audit_query_time_filters_compare_instants(tmp_path, backend: str) -> None:
    logger = _logger(tmp_path, backend)
    occurred_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    logger.record(
        _event(
            "same-instant",
            actor=Scope(org="acme", user="alice"),
            occurred_at=occurred_at,
        )
    )

    events = logger.query(
        {
            "occurred_after": "2026-01-01T13:00:00+01:00",
            "occurred_before": "2026-01-01T07:00:00-05:00",
        }
    )

    assert [event.id for event in events] == ["same-instant"]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_audit_query_time_filters_treat_naive_iso_as_utc(tmp_path, backend: str) -> None:
    logger = _logger(tmp_path, backend)
    occurred_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    logger.record(
        _event(
            "same-instant",
            actor=Scope(org="acme", user="alice"),
            occurred_at=occurred_at,
        )
    )

    events = logger.query(
        {
            "occurred_after": "2026-01-01T11:00:00",
            "occurred_before": "2026-01-01T13:00:00",
        }
    )

    assert [event.id for event in events] == ["same-instant"]


def test_sqlite_audit_logger_records_many_events(tmp_path) -> None:
    logger = SqliteAuditLogger(str(tmp_path / "audit.sqlite3"))
    actor = Scope(org="acme", user="alice")
    occurred_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    logger.record(_event("first", actor=actor, occurred_at=occurred_at))
    logger.record(_event("second", actor=actor, occurred_at=occurred_at))

    events = logger.query({}, limit=10)

    assert [event.id for event in events] == ["first", "second"]
