"""W-01.2: deterministic guards, real persisted units and failure isolation."""

import json
import math
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.common.type_def import DedupDecision, LifecycleState, Temporal, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.common.type_def.retrieval_filter import passes_lifecycle
from jiuwen_memory.construction.evolver import EvolveResult
from jiuwen_memory.construction.evolver_impl.dynamic_evolver import (
    ConsolidateDecision,
    DynamicEvolver,
)
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
    assert result.updated_ids == [existing.id]
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


@pytest.mark.parametrize("path", ["single", "batch", "batch_fallback", "dynamic"])
@pytest.mark.parametrize("decision", [DedupDecision.UPDATE, DedupDecision.SUPERSEDE])
def test_model_selected_target_keeps_its_own_similarity(path, decision):
    # pylint: disable=protected-access  # 白盒验证模型目标与分数在各判定路径中保持一致
    from tests.unit.construction.test_dynamic_extraction_consolidation import _ScriptedLLM

    first, selected, candidate = _fact("first", 1), _fact("selected", 2), _fact("c", 3)
    payload = {"candidate_id": "c", "existing_id": "selected", "decision": decision.value}
    responses = [json.dumps([payload] if path == "batch" else payload), "merged content"]
    if path == "batch_fallback":
        responses.insert(0, "invalid batch response")
    candidate.system_metadata.update({
        "_extraction_strategy": "fact", "_consolidation_prompt_fact": "test",
    })
    evolver, kv, _ = _make_evolver(
        kind=DynamicEvolver if path == "dynamic" else OrchestratingEvolver,
        llm=_ScriptedLLM(responses), dedup_hits=[(first, 0.85), (selected, 0.8)],
        prompts={"consolidate": {"test": "Judge the candidate"}},
    )
    for unit in (first, selected):
        kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    if path in ("batch", "batch_fallback"):
        result = evolver._dedup_batch([candidate])
    else:
        result = EvolveResult()
        if path == "dynamic":
            judged = evolver._judge(candidate, [(first, 0.85), (selected, 0.8)])
        else:
            judged = evolver._dedup_single(candidate)
        assert judged[1].id == "selected"
        assert math.isclose(judged[2], 0.8, rel_tol=1e-9, abs_tol=1e-12)
        evolver._apply_decision(candidate, *judged, result)
    assert loads(kv.get(first.scope, memory_key(first.id))) == first
    written_id = "selected" if decision == DedupDecision.UPDATE else "c"
    stored = loads(kv.get(candidate.scope, memory_key(written_id)))
    assert math.isclose(
        float(stored.system_metadata["dedup_similarity"]),
        0.8, rel_tol=1e-9, abs_tol=1e-12,
    )
    if decision == DedupDecision.UPDATE:
        assert result.updated_ids == ["selected"]
    else:
        assert result.superseded_ids == ["selected"] and stored.supersedes == "selected"


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


@pytest.mark.parametrize("kind", [OrchestratingEvolver, DynamicEvolver])
def test_noop_does_not_copy_or_write_memory(kind, monkeypatch):
    # pylint: disable=protected-access  # 白盒验证 NOOP 不进入副本与写入路径
    evolver, kv, index = _make_evolver(kind=kind)
    existing, candidate = _fact("e", 1), _fact("c", 2)
    before = deepcopy((existing, candidate))

    def unexpected_copy(_value):
        pytest.fail("NOOP must not copy memory objects")

    monkeypatch.setattr(
        "jiuwen_memory.construction.evolver_impl.orchestrating_evolver.deepcopy", unexpected_copy,
    )
    result = EvolveResult()
    assert evolver._apply_decision(candidate, DedupDecision.NOOP, existing, 0.8, result) == 1
    assert (existing, candidate) == before
    assert not index.built and not index.updated
    assert result == EvolveResult() and kv.list(candidate.scope).count == 0


def test_add_build_mutation_does_not_escape_to_caller(monkeypatch):
    # pylint: disable=protected-access  # 白盒注入索引回填并验证候选副本隔离
    evolver, _, index = _make_evolver()
    candidate = _fact("c", 1)
    before = deepcopy(candidate)
    build = index.build

    def mutate_then_build(units):
        units[0].segments[0].content = "index builder changed content"
        units[0].system_metadata["index_projection"] = "added"
        build(units)

    monkeypatch.setattr(index, "build", mutate_then_build)
    result = EvolveResult()
    evolver._apply_decision(candidate, DedupDecision.ADD, None, 0.0, result)
    assert candidate == before
    assert result.created_units[0].content == "index builder changed content"


@pytest.mark.parametrize("decision", [DedupDecision.UPDATE, DedupDecision.SUPERSEDE])
def test_refreshed_shared_target_is_not_modified_in_place(decision, monkeypatch):
    # pylint: disable=protected-access  # 白盒模拟真源返回共享对象，验证修改前复制
    from types import SimpleNamespace

    evolver, kv, _ = _make_evolver()
    existing, candidate = _fact("e", 1), _fact("c", 2)
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    before = deepcopy(existing)
    domain = SimpleNamespace(get=lambda scope, ids: [existing] if ids == [existing.id] else [])
    monkeypatch.setattr(OrchestratingEvolver, "_domain", property(lambda self: domain))

    evolver._apply_decision(candidate, decision, existing, 0.8, EvolveResult())

    assert existing == before, "重新读取的目标即使共享，也不能被原地修改"
    stored = loads(kv.get(existing.scope, memory_key(existing.id)))
    assert stored != before, "修改应通过副本正常写入真源"


@pytest.mark.parametrize("kind", [OrchestratingEvolver, DynamicEvolver])
@pytest.mark.parametrize("same_batch", [False, True])
@pytest.mark.parametrize("decision", [DedupDecision.UPDATE, DedupDecision.SUPERSEDE])
@pytest.mark.parametrize("followup_day", [2, 3, 4])
def test_update_advances_statement_time_for_followup_decisions(
    kind, same_batch, decision, followup_day,
):
    # pylint: disable=protected-access  # 验证连续写入与真源复核使用更新后的消息时间
    from tests.unit.construction.test_dynamic_extraction_consolidation import _ScriptedLLM

    evolver, kv, _ = _make_evolver(
        kind=kind, llm=_ScriptedLLM(["merged through day 3", "merged through day 4"]),
    )
    initial = _fact("initial", 1, start=datetime(2026, 4, 1, tzinfo=UTC))
    initial.temporal.t_event = datetime(2026, 4, 2, tzinfo=UTC)
    newer = _fact("newer", 3)
    result = EvolveResult()
    evolver._apply_decision(initial, DedupDecision.ADD, None, 0.0, result)
    evolver._apply_decision(newer, DedupDecision.UPDATE, initial, 0.8, result)
    merged = loads(kv.get(initial.scope, memory_key(initial.id)))
    assert merged.content == "merged through day 3"
    assert merged.temporal.t_message == newer.temporal.t_message
    assert merged.temporal.t_event == initial.temporal.t_event
    assert merged.temporal.t_valid == initial.temporal.t_valid
    assert initial.temporal.t_message == _fact("original", 1).temporal.t_message
    assert result.created_units[0] == merged, "批内已创建记录也必须同步最新陈述时间"

    if not same_batch:
        result = EvolveResult()
    followup = _fact("followup", followup_day)
    # 仍传入 day 1 的旧召回对象，要求执行前从真源重新读取 day 3 的记录。
    evolver._apply_decision(followup, decision, initial, 0.8, result)
    stored = loads(kv.get(initial.scope, memory_key(initial.id)))
    if followup_day <= 3:
        assert stored == merged, "迟到或同刻消息不能改写已合并的较新正文"
        saved = loads(kv.get(followup.scope, memory_key(followup.id)))
        expected = (
            LifecycleState.SUPERSEDED
            if followup_day < 3 and decision == DedupDecision.SUPERSEDE
            else LifecycleState.ACTIVE
        )
        assert saved.lifecycle == expected
        assert initial.id not in result.superseded_ids
    elif decision == DedupDecision.UPDATE:
        assert stored.content == "merged through day 4"
        assert stored.temporal.t_message == followup.temporal.t_message
        assert stored.lifecycle == LifecycleState.ACTIVE
    else:
        assert stored.lifecycle == LifecycleState.SUPERSEDED
        saved = loads(kv.get(followup.scope, memory_key(followup.id)))
        assert saved.lifecycle == LifecycleState.ACTIVE
        assert saved.supersedes == initial.id
        assert saved.temporal.t_message == followup.temporal.t_message


@pytest.mark.parametrize("kind", [OrchestratingEvolver, DynamicEvolver])
@pytest.mark.parametrize("dimension", ["session", "agent"])
@pytest.mark.parametrize("successor_id", ["newer", "a"])
def test_batch_successors_use_full_target_identity(kind, dimension, successor_id, monkeypatch):
    # pylint: disable=protected-access
    evolver, kv, _ = _make_evolver(kind=kind)
    initial, unrelated = _fact("a", 1), _fact("a", 1)
    unrelated.scope = replace(initial.scope, **{dimension: "unrelated"})
    newer, other_newer, middle = _fact(successor_id, 3), _fact("other", 4), _fact("middle", 2)
    newer.scope = replace(initial.scope, **{dimension: "successor"})
    other_newer.scope = replace(unrelated.scope, **{dimension: "other-successor"})
    for unit in (initial, unrelated):
        kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    latest = _fact("latest", 5)
    latest.scope = replace(initial.scope, **{dimension: "latest-successor"})
    candidates = [newer, other_newer, latest, middle]
    targets = {
        newer.id: initial, other_newer.id: unrelated, latest.id: initial, middle.id: initial,
    }

    def _target_of(unit):
        try:
            return targets[unit.id]
        except KeyError:
            raise AssertionError(f"missing mapped target for unit {unit.id}") from None

    if kind is DynamicEvolver:
        result = evolver._persist_decisions(candidates, [
            ConsolidateDecision(unit, DedupDecision.SUPERSEDE, _target_of(unit), 0.8)
            for unit in candidates
        ])
    else:
        monkeypatch.setattr(
            evolver._dedup, "recall", lambda unit: [(_target_of(unit), 0.8)]
        )

        def decide(items, *, targets):
            return {unit.id: DedupDecision.SUPERSEDE for unit, _ in items}

        monkeypatch.setattr(evolver, "_llm_dedup_decide_batch", decide)
        result = evolver._dedup_batch(candidates)
    stored = loads(kv.get(middle.scope, memory_key(middle.id)))
    assert stored.lifecycle == LifecycleState.SUPERSEDED
    assert stored.system_metadata["dedup_superseded_by"] == latest.id
    assert stored.system_metadata["dedup_temporal_reason"] == "older_statement"
    assert loads(kv.get(newer.scope, memory_key(newer.id))).lifecycle == LifecycleState.SUPERSEDED
    assert loads(kv.get(latest.scope, memory_key(latest.id))).lifecycle == LifecycleState.ACTIVE
    assert loads(kv.get(other_newer.scope, memory_key(other_newer.id))).lifecycle == (
        LifecycleState.ACTIVE
    )
    assert len(result.created_units) == 4


@pytest.mark.parametrize("decision", [DedupDecision.UPDATE, DedupDecision.SUPERSEDE])
def test_created_units_refresh_does_not_replace_same_id_in_another_scope(decision):
    # pylint: disable=protected-access
    evolver, kv, _ = _make_evolver()
    original, unrelated, candidate = _fact("a", 1), _fact("a", 1), _fact("b", 3)
    unrelated.scope = replace(original.scope, session="unrelated")
    unrelated.segments[0].content = "Independent same-ID record"
    for unit in (original, unrelated):
        kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    result = EvolveResult(created_units=[original, unrelated])
    evolver._apply_decision(candidate, decision, original, 0.8, result, successors={})
    assert result.created_units[1] == unrelated
    assert loads(kv.get(unrelated.scope, memory_key(unrelated.id))) == unrelated
    assert result.created_units[0] == loads(kv.get(original.scope, memory_key(original.id)))
