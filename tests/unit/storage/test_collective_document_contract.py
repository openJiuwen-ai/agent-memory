# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Behavior of collective extraction and orthogonal document paths."""
import json

import pytest

from jiuwen_memory.common.type_def import MD_TITLE_KEY, MemoryUnit, Segment
from jiuwen_memory.construction.extractor_impl.team_llm_extractor import parse_candidates
from jiuwen_memory.storage.markdown_impl._md_path import (
    class_from_md_path,
    collective_document_path,
    collective_document_target,
)

pytestmark = pytest.mark.unit


def test_collective_candidates_require_literal_human_evidence_for_profile():
    sources = [
        MemoryUnit(id="u", segments=[Segment("Prefer compact responses")],
                   system_metadata={"source_role": "user"}),
        MemoryUnit(id="a", segments=[Segment("Invented company name")],
                   system_metadata={"source_role": "assistant"}),
    ]
    candidates = parse_candidates(json.dumps([
        {"content": "made-up preference", "source_id": "u", "evidence": "Prefer compact responses",
         "destination": "user_profile", "kind": "user_fact",
         "title": "  Compact\nresponses  "},
        {"content": "invented", "source_id": "a", "evidence": "Invented company name",
         "destination": "user_profile", "kind": "user_fact"},
        {"content": "made-up profile another way", "source_id": "a", "evidence": "Invented company name",
         "destination": "user_profile", "kind": "experience"},
        {"content": "team decision", "source_id": "u", "evidence": "   ",
         "destination": "team", "kind": "experience"},
        {"content": "An unauthorized team diary", "source_id": "u",
         "evidence": "Prefer compact responses", "destination": "team", "kind": "daily"},
    ]), sources, {"user_profile": "", "team": "team-1"})
    assert [(item.content, item.system_metadata["collective_destination"]) for item in candidates] == [
        ("Prefer compact responses", "user_profile"),
    ]
    assert candidates[0].system_metadata[MD_TITLE_KEY] == "Compact responses"


def test_collective_user_selection_preserves_literal_answer_and_selected_meaning():
    source = MemoryUnit(
        id="answer", segments=[Segment("A")],
        system_metadata={
            "source_role": "user",
            "source_selection": {"question": "Preferred handoff format?", "description": "PDF"},
        },
    )
    candidates = parse_candidates(json.dumps([{
        "content": "The user runs a design studio", "source_id": "answer", "evidence": "A",
        "destination": "user_profile", "kind": "user_fact",
    }]), [source], {"user_profile"})
    [candidate] = candidates
    assert candidate.content == "Preferred handoff format? → A (PDF)"


@pytest.mark.parametrize("kind,key,path", [
    ("project", "P", "memory/team_memory/projects/P/MEMORY.md"),
    ("team", "T", "memory/team_memory/teams/T/MEMORY.md"),
    ("group", "G", "memory/team_memory/groups/G/MEMORY.md"),
    ("personal", "pkg:Coder", "memory/team_memory/agents/pkg_Coder/MEMORY.md"),
    ("personal", "pkg_Coder", "memory/team_memory/agents/pkg%5FCoder/MEMORY.md"),
])
def test_collective_path_round_trip(kind, key, path):
    assert collective_document_path(kind, key) == path
    assert collective_document_target(path) == (kind, key)


def test_collective_project_diary_path_round_trip():
    path = "memory/team_memory/projects/P/daily_memory/2026-11-24.md"
    assert collective_document_target(path) == ("project", "P")
    assert class_from_md_path(path) == "daily_memory"
    assert collective_document_path("project", "P", "2026-11-24.md") == path
