# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Behavior of collective extraction and orthogonal document paths."""
import asyncio
import json
from contextlib import closing

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import MD_TITLE_KEY, MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.memory_filter import collective_destination_of
from jiuwen_memory.construction.extractor_impl.team_llm_extractor import parse_candidates
from jiuwen_memory.storage.markdown_impl._md_path import (
    class_from_md_path,
    collective_document_path,
    collective_document_target,
    collective_landing_metadata,
)
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import SqliteDocumentShadowIndex
from jiuwen_memory.storage.watchdog_impl.local_watchdog import LocalWatchdog

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
        {"content": "made-up profile another way", "source_id": "a",
         "evidence": "Invented company name",
         "destination": "user_profile", "kind": "experience"},
        {"content": "team decision", "source_id": "u", "evidence": "   ",
         "destination": "team", "kind": "experience"},
        {"content": "An unauthorized team diary", "source_id": "u",
         "evidence": "Prefer compact responses", "destination": "team", "kind": "daily"},
    ]), sources, {"user_profile": "", "team": "team-1"})
    assert [
        (item.content, item.system_metadata["collective_destination"]) for item in candidates
    ] == [("Prefer compact responses", "user_profile")]
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
    ("agent", "pkg:Coder", "memory/team_memory/agents/pkg_Coder/MEMORY.md"),
    ("agent", "pkg_Coder", "memory/team_memory/agents/pkg%5FCoder/MEMORY.md"),
])
def test_collective_path_round_trip(kind, key, path):
    assert collective_document_path(kind, key) == path
    assert collective_document_target(path) == (kind, key)


def test_collective_agent_memory_class_matches_write_and_document_recovery(tmp_path):
    agent = "pkg:Coder"
    scope = Scope(agent=agent)
    path = collective_document_path("agent", agent)
    metadata = collective_landing_metadata("agent", agent)
    document = tmp_path / path
    document.parent.mkdir(parents=True)
    document.write_text("manual\n", encoding="utf-8")

    async def recover():
        shadow = SqliteDocumentShadowIndex(":memory:", tokenizer=WhitespaceTokenizer())
        with closing(shadow):
            watchdog = LocalWatchdog(
                shadow, str(tmp_path), scope, loop=asyncio.get_running_loop(), debounce_ms=1,
            )
            try:
                watchdog.schedule_sync(str(document))
                async with asyncio.timeout(5):
                    rows = shadow.list_units_by_md(scope, path)
                    while not rows:
                        await asyncio.sleep(0.01)
                        rows = shadow.list_units_by_md(scope, path)
                [recovered] = shadow.get_units(scope, [unit_id for unit_id, _ in rows])
                return recovered
            finally:
                watchdog.stop()

    recovered = asyncio.run(recover())
    assert metadata["collective_destination"] == "agent"
    assert metadata["memory_class"] == "agent_memory"
    assert class_from_md_path(path) == "agent_memory"
    assert metadata["shareable"] is True
    assert collective_destination_of({"md_filename": path}) == "agent"
    assert recovered.scope == scope
    assert recovered.content == "manual"
    assert all(recovered.system_metadata[key] == value for key, value in metadata.items())
    with pytest.raises(ValidationError, match="Invalid collective document coordinate"):
        collective_document_path("personal", agent)


def test_collective_project_diary_path_round_trip():
    path = "memory/team_memory/projects/P/daily_memory/2026-11-24.md"
    assert collective_document_target(path) == ("project", "P")
    assert class_from_md_path(path) == "daily_memory"
    assert collective_document_path("project", "P", "2026-11-24.md") == path
