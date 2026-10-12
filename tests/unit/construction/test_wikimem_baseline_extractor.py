"""wikimem baseline candidate extractor compatibility tests."""

from __future__ import annotations

import pytest

from common.type_def import MemoryTier
from construction.extractor_impl.wikimem_baseline_extractor import (
    WIKIMEM_ACTION,
    WIKIMEM_KEY,
    WIKIMEM_MEMORY_TYPE,
    WIKIMEM_PREFERRED_SCOPE,
    WIKIMEM_SCORE,
    WIKIMEM_SKIP_REASON,
    WIKIMEM_SOURCE_MESSAGE_ID,
    WIKIMEM_VALUE,
    WikimemBaselineExtractor,
)

from tests.unit.construction.fixtures import create_test_unit

pytestmark = pytest.mark.unit


def _candidate_by_key(candidates, key: str):
    return next(item for item in candidates if item.metadata[WIKIMEM_KEY] == key)


def test_extracts_key_value_candidates_with_type_score_and_source() -> None:
    source = create_test_unit("msg-1", "preference: must run staging checks")
    extractor = WikimemBaselineExtractor()

    candidates = extractor.extract([source])

    candidate = _candidate_by_key(candidates, "preference")
    assert candidate.tier == MemoryTier.SEMANTIC
    assert candidate.provenance == ["msg-1"]
    assert candidate.metadata[WIKIMEM_ACTION] == "upsert"
    assert candidate.metadata[WIKIMEM_VALUE] == "must run staging checks"
    assert candidate.metadata[WIKIMEM_MEMORY_TYPE] == "feedback"
    assert candidate.metadata[WIKIMEM_SCORE] == "1.25"
    assert candidate.metadata[WIKIMEM_SOURCE_MESSAGE_ID] == "msg-1"


def test_ignores_interrogative_key_value_text() -> None:
    source = create_test_unit("msg-1", "what: should I remember")

    assert WikimemBaselineExtractor().extract([source]) == []


def test_extracts_explicit_remember_with_scope_hint_and_stable_note_key() -> None:
    source = create_test_unit(
        "msg-2",
        "remember for team memory Release gate must run staging smoke tests.",
    )

    [candidate] = WikimemBaselineExtractor().extract([source])

    assert candidate.metadata[WIKIMEM_ACTION] == "upsert"
    assert candidate.metadata[WIKIMEM_KEY] == "memory_note_release_gate_must_run"
    assert candidate.metadata[WIKIMEM_VALUE] == "Release gate must run staging smoke tests"
    assert candidate.metadata[WIKIMEM_PREFERRED_SCOPE] == "team"
    assert candidate.metadata[WIKIMEM_MEMORY_TYPE] == "feedback"


def test_extracts_chinese_scope_hint() -> None:
    source = create_test_unit("msg-3", "请记住团队共享 release gate must run")

    [candidate] = WikimemBaselineExtractor().extract([source])

    assert candidate.metadata[WIKIMEM_PREFERRED_SCOPE] == "team"
    assert candidate.metadata[WIKIMEM_KEY] == "memory_note_release_gate_must_run"


def test_extracts_explicit_forget_by_multiword_target() -> None:
    source = create_test_unit("msg-4", "forget the Release gate must run staging smoke tests")

    [candidate] = WikimemBaselineExtractor().extract([source])

    assert candidate.metadata[WIKIMEM_ACTION] == "forget"
    assert candidate.metadata[WIKIMEM_KEY] == "memory_note_release_gate_must_run"
    assert candidate.metadata[WIKIMEM_VALUE] == "Release gate must run staging smoke tests"
    assert WIKIMEM_MEMORY_TYPE not in candidate.metadata


def test_team_secret_candidate_keeps_diagnostic_skip_reason() -> None:
    source = create_test_unit("msg-5", "remember for team memory token is github_pat_secret")

    [candidate] = WikimemBaselineExtractor().extract([source])

    assert candidate.metadata[WIKIMEM_PREFERRED_SCOPE] == "team"
    assert candidate.metadata[WIKIMEM_SKIP_REASON] == "potential_secret"
