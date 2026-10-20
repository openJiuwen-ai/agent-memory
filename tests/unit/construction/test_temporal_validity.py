"""W-01.2: deterministic guards, real persisted units and failure isolation."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.common.type_def import DedupDecision, LifecycleState, Temporal, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.common.type_def.retrieval_filter import passes_lifecycle
from jiuwen_memory.construction.evolver import EvolveResult
from jiuwen_memory.construction.evolver_impl.dynamic_evolver import DynamicEvolver
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import OrchestratingEvolver
from jiuwen_memory.construction.evolver_impl.temporal_validity import compare_validity
from tests.unit.construction.test_dynamic_extraction_consolidation import _make_evolver, _unit

pytestmark = pytest.mark.unit
NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _fact(uid, day, *, start=None, end=None):
    unit = _unit(uid, f"Alice lives in city {uid}")
    unit.temporal = Temporal(
        t_message=datetime(2026, 5, day, tzinfo=UTC) if day else None,
        t_valid=start,
        t_invalid=end,
    )
    return unit


@pytest.mark.parametrize(
    ("candidate_day", "existing_day", "action", "reason"),
    [
        (1, 2, "older", "older_statement"),
        (2, 1, "newer", "newer_statement"),
        (1, 1, "preserve", "equal_message_time"),
        (None, 2, "preserve", "unknown_message_time"),
        (2, None, "preserve", "unknown_message_time"),
    ],
)
def test_statement_order(candidate_day, existing_day, action, reason):
    verdict = compare_validity(_fact("c", candidate_day), _fact("e", existing_day), now=NOW)
    assert (verdict.action, verdict.reason) == (action, reason)


@pytest.mark.parametrize("side", ["candidate", "existing"])
@pytest.mark.parametrize("offset_seconds", [0, 1, 300])
def test_future_message_boundary_has_no_implicit_clock_skew(side, offset_seconds):
    candidate, existing = _fact("c", 2), _fact("e", 1)
    unit = candidate if side == "candidate" else existing
    unit.temporal.t_message = NOW + timedelta(seconds=offset_seconds)

    verdict = compare_validity(candidate, existing, now=NOW)

    if offset_seconds > 0:
        assert (verdict.action, verdict.reason) == ("preserve", "future_message_time")
    else:
        expected = "newer" if side == "candidate" else "older"
        assert verdict.action == expected, "消息时间恰好等于 now 时仍可参与排序"


def test_event_dates_do_not_define_event_identity_or_statement_order():
    candidate, existing = _fact("c", 1), _fact("e", 2)
    candidate.temporal.t_event = datetime(2026, 6, 1, tzinfo=UTC)
    existing.temporal.t_event = datetime(2026, 5, 1, tzinfo=UTC)
    assert compare_validity(candidate, existing, now=NOW).action == "older"
    candidate.temporal.t_message = existing.temporal.t_message = None
    assert compare_validity(candidate, existing, now=NOW).action == "preserve"


def test_future_event_without_future_validity_does_not_retire_current_state():
    candidate = _fact("c", 2)
    candidate.temporal.t_event = NOW + timedelta(days=30)
    assert compare_validity(candidate, _fact("e", 1), now=NOW).reason == (
        "future_event_requires_validity"
    )


@pytest.mark.parametrize("field", ["t_message", "t_valid", "t_invalid"])
def test_naive_timezone_is_unknown(field):
    candidate = _fact("c", 1)
    setattr(candidate.temporal, field, datetime(2026, 5, 1))
    assert compare_validity(candidate, _fact("e", 2), now=NOW).action == "preserve"


def test_aware_timezones_compare_as_instants():
    candidate, existing = _fact("c", 1), _fact("e", 1)
    candidate.temporal.t_message = datetime.fromisoformat("2026-05-01T09:00:00+08:00")
    existing.temporal.t_message = datetime.fromisoformat("2026-05-01T02:00:00+00:00")
    assert compare_validity(candidate, existing, now=NOW).action == "older"


@pytest.mark.parametrize("end", [NOW, NOW - timedelta(days=1)])
def test_empty_or_inverted_interval_preserved(end):
    candidate = _fact("c", 1, start=NOW, end=end)
    assert compare_validity(candidate, _fact("e", 2), now=NOW).reason == (
        "invalid_validity_interval"
    )


def test_disjoint_half_open_intervals_preserved():
    candidate = _fact("c", 1, start=NOW - timedelta(days=2), end=NOW)
    existing = _fact("e", 2, start=NOW)
    assert compare_validity(candidate, existing, now=NOW).reason == "disjoint_validity"


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("decision", [DedupDecision.SUPERSEDE, DedupDecision.UPDATE])
def test_old_statement_cannot_change_new_fact(dynamic, decision):
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    kind = DynamicEvolver if dynamic else OrchestratingEvolver
    evolver, kv, index = _make_evolver(kind=kind)
    candidate = _fact("candidate", 1, start=datetime(2026, 6, 1, tzinfo=UTC))
    existing = _fact("existing", 2)
    original = dumps(existing)
    kv.insert(existing.scope, memory_key(existing.id), original)
    result = EvolveResult()
    evolver._apply_decision(candidate, decision, existing, 0.8, result)
    stored = loads(kv.get(candidate.scope, memory_key(candidate.id)))
    assert kv.get(existing.scope, memory_key(existing.id)) == original
    assert not index.updated
    assert result.created_ids == [candidate.id]
    assert result.created_units == [stored]
    assert candidate.lifecycle == LifecycleState.ACTIVE, "caller object must stay unchanged"
    if decision == DedupDecision.SUPERSEDE:
        assert stored.lifecycle == LifecycleState.SUPERSEDED
        assert stored.temporal.t_invalid > stored.temporal.t_valid
        assert not stored.supersedes, "old record must not claim it replaces the new record"
        assert stored.system_metadata["dedup_superseded_by"] == existing.id
        assert not passes_lifecycle(stored, None)
        assert passes_lifecycle(stored, datetime(2026, 6, 2, tzinfo=UTC))
        assert result.superseded_ids == [candidate.id]
    else:
        assert stored.lifecycle == LifecycleState.ACTIVE, "preserve complementary old details"


@pytest.mark.parametrize("decision", [DedupDecision.SUPERSEDE, DedupDecision.UPDATE])
def test_missing_or_equal_time_preserves_both(decision):
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    evolver, kv, index = _make_evolver()
    existing = _fact("e", None)
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    result = EvolveResult()
    evolver._apply_decision(_fact("c", 1), decision, existing, 0.8, result)
    assert not index.updated
    assert result.created_ids == ["c"]
    assert not result.superseded_ids


def test_future_validity_does_not_retire_current_fact_early():
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    evolver, kv, _ = _make_evolver()
    existing = _fact("e", 1)
    future = datetime.now(UTC) + timedelta(days=30)
    candidate = _fact("c", 2, start=future)
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    result = EvolveResult()
    evolver._apply_decision(candidate, DedupDecision.SUPERSEDE, existing, 0.8, result)
    old = loads(kv.get(existing.scope, memory_key(existing.id)))
    new = loads(kv.get(candidate.scope, memory_key(candidate.id)))
    assert passes_lifecycle(old, None)
    assert not passes_lifecycle(new, None)
    assert not passes_lifecycle(old, future)
    assert passes_lifecycle(new, future)
    assert old.temporal.t_invalid == future
    assert not result.superseded_ids


def test_failed_build_does_not_mutate_existing_or_candidate(monkeypatch):
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    evolver, kv, index = _make_evolver()
    existing, candidate = _fact("e", 1), _fact("c", 2)
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    before = deepcopy((existing, candidate))

    def fail(_units):
        raise RuntimeError("injected build failure")

    monkeypatch.setattr(index, "build", fail)
    with pytest.raises(RuntimeError, match="injected build failure"):
        evolver._apply_decision(candidate, DedupDecision.SUPERSEDE, existing, 0.8, EvolveResult())
    assert (existing, candidate) == before
    assert loads(kv.get(existing.scope, memory_key(existing.id))) == existing


def test_batch_rechecks_superseded_target():
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    evolver, kv, _ = _make_evolver()
    initial, newer, middle = _fact("a", 1), _fact("c", 3), _fact("b", 2)
    kv.insert(initial.scope, memory_key(initial.id), dumps(initial))
    result = EvolveResult()
    evolver._apply_decision(newer, DedupDecision.SUPERSEDE, initial, 0.8, result)
    evolver._apply_decision(middle, DedupDecision.SUPERSEDE, initial, 0.8, result)
    assert loads(kv.get(newer.scope, memory_key(newer.id))).lifecycle == LifecycleState.ACTIVE
    assert loads(kv.get(middle.scope, memory_key(middle.id))).lifecycle == LifecycleState.SUPERSEDED


def test_target_must_be_visible_and_unambiguous():
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    hits = [(_fact("a", 1), 0.9), (_fact("b", 2), 0.8)]
    assert OrchestratingEvolver._validated_target(
        {"existing_id": "b"}, hits, DedupDecision.SUPERSEDE
    ).id == "b"
    for payload in ({}, {"existing_id": "unlisted"}):
        with pytest.raises(ValueError, match="listed existing_id"):
            OrchestratingEvolver._validated_target(payload, hits, DedupDecision.UPDATE)


def test_old_statement_remains_current_until_known_future_successor_starts():
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    evolver, kv, _ = _make_evolver()
    future = datetime.now(UTC) + timedelta(days=30)
    existing = _fact("future", 2, start=future)
    candidate = _fact("current", 1, start=NOW)
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    result = EvolveResult()
    evolver._apply_decision(candidate, DedupDecision.SUPERSEDE, existing, 0.8, result)
    stored = loads(kv.get(candidate.scope, memory_key(candidate.id)))
    assert passes_lifecycle(stored, None)
    assert stored.temporal.t_invalid == future
    assert not passes_lifecycle(stored, future)


def test_reverse_expiry_retry_reuses_persisted_unit():
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    evolver, kv, _ = _make_evolver()
    existing, candidate = _fact("e", 2), _fact("c", 1)
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    for _ in range(2):
        result = EvolveResult()
        evolver._apply_decision(candidate, DedupDecision.SUPERSEDE, existing, 0.8, result)
        assert result.created_ids == [candidate.id]
        assert result.superseded_ids == [candidate.id]
    assert loads(kv.get(candidate.scope, memory_key(candidate.id))).lifecycle == (
        LifecycleState.SUPERSEDED
    )


def test_batch_modifies_model_selected_target_instead_of_highest_score():
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    from tests.unit.construction.test_dynamic_extraction_consolidation import _ScriptedLLM

    first, selected, candidate = _fact("first", 1), _fact("selected", 2), _fact("c", 3)
    llm = _ScriptedLLM([json.dumps([
        {"candidate_id": "c", "existing_id": "selected", "decision": "supersede"}
    ])])
    evolver, kv, _ = _make_evolver(llm=llm, dedup_hits=[(first, 0.85), (selected, 0.8)])
    for unit in (first, selected):
        kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    result = evolver._dedup_batch([candidate])
    assert result.superseded_ids == ["selected"]
    assert loads(kv.get(first.scope, memory_key(first.id))).lifecycle == LifecycleState.ACTIVE
    assert loads(kv.get(candidate.scope, memory_key(candidate.id))).supersedes == "selected"


def test_retry_finishes_retirement_after_partial_write(monkeypatch):
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    evolver, kv, index = _make_evolver()
    existing, candidate = _fact("e", 1), _fact("c", 2)
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    update = index.update
    failures = []

    def fail_once(units):
        if units[0].id == "e" and not failures:
            failures.append(True)
            raise RuntimeError("retirement failed")
        update(units)

    monkeypatch.setattr(index, "update", fail_once)
    with pytest.raises(RuntimeError, match="retirement failed"):
        evolver._apply_decision(candidate, DedupDecision.SUPERSEDE, existing, 0.8, EvolveResult())
    assert loads(kv.get(existing.scope, memory_key(existing.id))).lifecycle == LifecycleState.ACTIVE
    result = EvolveResult()
    evolver._apply_decision(candidate, DedupDecision.SUPERSEDE, existing, 0.8, result)
    assert loads(kv.get(existing.scope, memory_key(existing.id))).lifecycle == (
        LifecycleState.SUPERSEDED
    )
    assert result.created_ids == ["c"]
