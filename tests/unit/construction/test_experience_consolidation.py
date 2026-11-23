# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""New-entry gating for EXPERIENCE_CONSOLIDATE, independent of scheduling."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.experience import (
    ATTR_HARMFUL,
    ATTR_HELPFUL,
    ATTR_NEUTRAL,
    AttributionEvent,
    UtilityStore,
)
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.memory import MemoryTier
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.curator import CurateOutcome, Curator
from jiuwen_memory.construction.evolver import EvolveMode
from jiuwen_memory.construction.experience_consolidation import (
    PROGRESS_KEY,
    ExperienceConsolidationProgress,
)
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
from tests.support.whitebox import private, set_private
from tests.unit.construction.test_experience_curator import _make_curator, _make_evolver

pytestmark = pytest.mark.unit
SCOPE = Scope(org="acme", space="project-a")


def entry(name, scope=None, tier=MemoryTier.SEMANTIC):
    return MemoryUnit(
        id=name, scope=deepcopy(scope or SCOPE), tier=tier,
        segments=[Segment(content="reusable lesson")],
        system_metadata={"memory_type": "experience", "category": "other"},
    )


class RecordingCurator(Curator):
    def __init__(self):
        self.calls = []
        self.success = True
        self.output = None
        self.on_call = None

    def operator_type(self):
        return OperatorType.CURATOR

    def health(self):
        return None

    def curate(self, units):
        return self.curate_with_status(units).units

    def curate_with_status(self, units):
        self.calls.append([u.id for u in units])
        if self.on_call:
            self.on_call()
        return CurateOutcome(list(units) if self.output is None else self.output, self.success)


def test_first_round_then_skip_then_new_entry_includes_old_entries():
    curator = RecordingCurator()
    evolver = _make_evolver(curator)
    old = entry("old")
    evolver.evolve([old], EvolveMode.EXPERIENCE_CONSOLIDATE)
    evolver.evolve([old], EvolveMode.EXPERIENCE_CONSOLIDATE)
    assert curator.calls == [["old"]]
    evolver.evolve([old, entry("new")], EvolveMode.EXPERIENCE_CONSOLIDATE)
    assert curator.calls == [["old"], ["old", "new"]]
    abstractor_calls = private(evolver, "_abstractor").calls
    assert [[u.id for u in batch] for batch in abstractor_calls] == curator.calls


def test_progress_survives_evolver_and_sqlite_recreation(tmp_path):
    path = str(tmp_path / "progress.sqlite")
    first = _make_evolver(RecordingCurator())
    first_store = SQLiteKVStore(path)
    set_private(first, "_message_store", first_store)
    first.evolve([entry("old")], EvolveMode.EXPERIENCE_CONSOLIDATE)
    first_store.close()
    later_curator = RecordingCurator()
    later = _make_evolver(later_curator)
    later_store = SQLiteKVStore(path)
    set_private(later, "_message_store", later_store)
    try:
        later.evolve([entry("old")], EvolveMode.EXPERIENCE_CONSOLIDATE)
        assert later_curator.calls == []
        later.evolve([entry("old"), entry("new")], EvolveMode.EXPERIENCE_CONSOLIDATE)
        assert later_curator.calls == [["old", "new"]]
    finally:
        later_store.close()


def test_progress_is_isolated_by_org_and_space():
    curator = RecordingCurator()
    evolver = _make_evolver(curator)
    scopes = [SCOPE, Scope(org="acme", space="project-b"), Scope(org="other", space="project-a")]
    batch = [entry("same-id", scope) for scope in scopes]
    evolver.evolve(batch, EvolveMode.EXPERIENCE_CONSOLIDATE)
    assert curator.calls == [["same-id"]] * 3
    evolver.evolve(batch, EvolveMode.EXPERIENCE_CONSOLIDATE)
    assert len(curator.calls) == 3


@pytest.mark.parametrize("failure", ["curator", "abstractor", "persist", "progress"])
def test_failure_does_not_acknowledge_inputs(monkeypatch, failure):
    curator = RecordingCurator()
    evolver = _make_evolver(curator)
    if failure == "curator":
        curator.success = False
        evolver.evolve([entry("new")], EvolveMode.EXPERIENCE_CONSOLIDATE)
    else:
        def fail(*_args, **_kwargs):
            raise RuntimeError("round failed")
        if failure == "abstractor":
            monkeypatch.setattr(private(evolver, "_abstractor"), "abstract", fail)
        elif failure == "persist":
            abstractor = SimpleNamespace(abstract=lambda units: units, dedup_products=False)
            set_private(evolver, "_abstractor", abstractor)
            monkeypatch.setattr(private(evolver, "_index"), "build", fail)
        else:
            monkeypatch.setattr(ExperienceConsolidationProgress, "save", fail)
        with pytest.raises(RuntimeError, match="round failed"):
            evolver.evolve([entry("new")], EvolveMode.EXPERIENCE_CONSOLIDATE)
    with pytest.raises(NotFoundError):
        private(evolver, "_message_store").get(SCOPE, PROGRESS_KEY)
    curator.success = True
    monkeypatch.undo()
    abstractor = SimpleNamespace(abstract=lambda units: [], dedup_products=False)
    set_private(evolver, "_abstractor", abstractor)
    evolver.evolve([entry("new")], EvolveMode.EXPERIENCE_CONSOLIDATE)
    assert len(curator.calls) == 2


def test_successfully_retiring_every_entry_still_advances_progress():
    curator = RecordingCurator()
    curator.output = []
    evolver = _make_evolver(curator)
    evolver.evolve([entry("old")], EvolveMode.EXPERIENCE_CONSOLIDATE)
    evolver.evolve([entry("old")], EvolveMode.EXPERIENCE_CONSOLIDATE)
    assert curator.calls == [["old"]]


def test_arrival_during_round_remains_new_next_time():
    curator = RecordingCurator()
    evolver = _make_evolver(curator)
    storage = [entry("old")]

    def add_during_round():
        storage.append(entry("arrived-during-round"))

    curator.on_call = add_during_round
    evolver.evolve(list(storage), EvolveMode.EXPERIENCE_CONSOLIDATE)
    known = ExperienceConsolidationProgress(private(evolver, "_message_store")).load(SCOPE)
    assert "arrived-during-round" not in known.entry_ids
    curator.on_call = None
    evolver.evolve(storage, EvolveMode.EXPERIENCE_CONSOLIDATE)
    assert curator.calls[-1] == ["old", "arrived-during-round"]


def test_merge_products_and_run_summaries_do_not_retrigger():
    curator = RecordingCurator()
    curator.output = [entry("merged")]
    evolver = _make_evolver(curator)
    evolver.evolve([entry("old")], EvolveMode.EXPERIENCE_CONSOLIDATE)
    evolver.evolve(
        [entry("merged"), entry("summary", tier=MemoryTier.EPISODIC)],
        EvolveMode.EXPERIENCE_CONSOLIDATE,
    )
    assert curator.calls == [["old"]]


def test_feedback_without_new_id_skips_but_author_erasure_is_not_blocked():
    curator = RecordingCurator()
    evolver = _make_evolver(curator)
    unit = entry("old")
    evolver.evolve([unit], EvolveMode.EXPERIENCE_CONSOLIDATE)
    unit.system_metadata["feedback"] = "updated"
    evolver.evolve([unit], EvolveMode.EXPERIENCE_CONSOLIDATE)
    assert len(curator.calls) == 1
    UtilityStore(private(evolver, "_message_store")).add_erased_author(SCOPE, "deleted-author")
    evolver.evolve([unit], EvolveMode.EXPERIENCE_CONSOLIDATE)
    assert len(curator.calls) == 2


def test_harmful_feedback_retries_and_does_not_acknowledge_concurrent_arrivals():
    curator = RecordingCurator()
    evolver = _make_evolver(curator)
    utility = UtilityStore(private(evolver, "_message_store"))
    unit = entry("old")
    mode = EvolveMode.EXPERIENCE_CONSOLIDATE

    def feedback(trajectory, verdict, revision=0):
        utility.write_attribution_event(
            SCOPE, "old", "alice", trajectory, revision, AttributionEvent(verdict=verdict)
        )

    def add_concurrent_feedback():
        feedback("concurrent", ATTR_HARMFUL)

    evolver.evolve([unit], mode)
    feedback("positive", ATTR_HELPFUL)
    feedback("corrected", ATTR_HARMFUL)
    feedback("corrected", ATTR_NEUTRAL, revision=1)
    evolver.evolve([unit], mode)
    assert len(curator.calls) == 1

    feedback("harm", ATTR_HARMFUL)
    curator.success = False
    evolver.evolve([unit], mode)
    curator.success = True
    curator.on_call = add_concurrent_feedback
    evolver.evolve([unit], mode)
    curator.on_call = None
    evolver.evolve([unit], mode)
    evolver.evolve([unit], mode)
    assert len(curator.calls) == 4


def test_plain_consolidate_keeps_its_existing_behavior():
    curator = RecordingCurator()
    evolver = _make_evolver(curator)
    for _ in range(2):
        evolver.evolve([entry("old")], EvolveMode.CONSOLIDATE)
    assert curator.calls == [["old"], ["old"]]
    with pytest.raises(NotFoundError):
        private(evolver, "_message_store").get(SCOPE, PROGRESS_KEY)


def test_corrupt_progress_is_reported_not_silently_overwritten():
    evolver = _make_evolver(RecordingCurator())
    private(evolver, "_message_store").insert(SCOPE, PROGRESS_KEY, b"invalid-json")
    with pytest.raises(ValidationError, match="progress"):
        evolver.evolve([entry("new")], EvolveMode.EXPERIENCE_CONSOLIDATE)


def test_real_curator_reports_failure_and_successful_empty_result(monkeypatch):
    curator, _builder, _store = _make_curator()
    # An incomplete procedural entry is intentionally retired, not a failed run.
    incomplete = entry("incomplete", tier=MemoryTier.PROCEDURAL)
    outcome = curator.curate_with_status([incomplete])
    assert outcome.succeeded and outcome.units == []

    def fail(*_args):
        raise RuntimeError("aggregation failed")

    monkeypatch.setattr(curator, "_aggregate", fail)
    outcome = curator.curate_with_status([entry("valid")])
    assert not outcome.succeeded


def test_skipped_merge_cluster_does_not_acknowledge_round(monkeypatch):
    curator, _builder, _store = _make_curator()
    evolver = _make_evolver(curator)

    def partial(_scope, units, _stats, summary):
        summary["skipped_clusters"] = 1
        return units

    monkeypatch.setattr(curator, "_merge_and_delete", partial)
    evolver.evolve([entry("new")], EvolveMode.EXPERIENCE_CONSOLIDATE)
    with pytest.raises(NotFoundError):
        private(evolver, "_message_store").get(SCOPE, PROGRESS_KEY)
    # The old curate contract still returns retained entries for ordinary callers.
    assert curator.curate([entry("new")])
