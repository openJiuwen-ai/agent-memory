"""wikimem baseline consolidation compatibility tests."""

from __future__ import annotations

import pytest

from common.type_def import LifecycleState
from construction.evolver_impl.wikimem_baseline_evolver import (
    WIKIMEM_DESCRIPTION,
    WikimemBaselineEvolver,
    is_wikimem_record,
)
from construction.extractor_impl.wikimem_baseline_extractor import (
    WIKIMEM_KEY,
    WIKIMEM_MEMORY_TYPE,
    WIKIMEM_SCORE,
    WIKIMEM_SKIP_REASON,
    WIKIMEM_SOURCE_MESSAGE_ID,
    WikimemBaselineExtractor,
)
from tests.unit.construction.fixtures import create_test_unit

pytestmark = pytest.mark.unit


def _candidate(text: str, source_id: str = "msg-1"):
    [candidate] = WikimemBaselineExtractor().extract([create_test_unit(source_id, text)])
    return candidate


def _record_by_key(units, key: str):
    return next(unit for unit in units if unit.metadata.get(WIKIMEM_KEY) == key)


def test_consolidate_upsert_creates_wikimem_record() -> None:
    candidate = _candidate("preference: must run staging checks", "msg-1")

    outcome = WikimemBaselineEvolver().consolidate([], [candidate])

    assert outcome.result.created_ids
    [record] = outcome.units
    assert is_wikimem_record(record)
    assert record.lifecycle == LifecycleState.ACTIVE
    assert record.content == "must run staging checks"
    assert record.metadata[WIKIMEM_KEY] == "preference"
    assert record.metadata[WIKIMEM_MEMORY_TYPE] == "feedback"
    assert record.metadata[WIKIMEM_SCORE] == "1.25"
    assert record.metadata[WIKIMEM_SOURCE_MESSAGE_ID] == "msg-1"
    assert "remembered feedback context" in record.metadata[WIKIMEM_DESCRIPTION]


def test_consolidate_upsert_supersedes_existing_key() -> None:
    old = WikimemBaselineEvolver().consolidate(
        [],
        [_candidate("preference: use old staging gate", "msg-1")],
    ).units
    new_candidate = _candidate("preference: use new staging gate", "msg-2")

    outcome = WikimemBaselineEvolver().consolidate(old, [new_candidate])

    old_record = next(unit for unit in outcome.units if unit.id == old[0].id)
    new_record = _record_by_key(
        [unit for unit in outcome.units if unit.lifecycle == LifecycleState.ACTIVE],
        "preference",
    )
    assert old_record.lifecycle == LifecycleState.SUPERSEDED
    assert new_record.supersedes == old_record.id
    assert new_record.content == "use new staging gate"
    assert old_record.id in outcome.result.superseded_ids


def test_consolidate_forget_marks_key_and_normalized_value_forgotten() -> None:
    prior = WikimemBaselineEvolver().consolidate(
        [],
        [_candidate("remember Release gate must run staging smoke tests", "msg-1")],
    ).units
    forget = _candidate("forget the Release gate must run staging smoke tests", "msg-2")

    outcome = WikimemBaselineEvolver().consolidate(prior, [forget])

    assert outcome.units[0].lifecycle == LifecycleState.FORGOTTEN
    assert outcome.units[0].id in outcome.result.forgotten_ids


def test_consolidate_skips_team_secret_candidate_with_diagnostic() -> None:
    candidate = _candidate("remember for team memory token is github_pat_secret", "msg-1")
    assert candidate.metadata[WIKIMEM_SKIP_REASON] == "potential_secret"

    outcome = WikimemBaselineEvolver().consolidate([], [candidate])

    assert outcome.units == []
    assert outcome.skipped_ids == [candidate.id]
