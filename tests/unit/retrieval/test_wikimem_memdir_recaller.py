"""wikimem memdir Recaller integration tests."""

from __future__ import annotations

import json

import pytest

from retrieval.recaller_impl.wikimem_memdir_recaller import (
    WikimemMemdirRecaller,
    memory_file_unit_id,
)
from retrieval.types import ParsedQuery, RecallChannel

pytestmark = pytest.mark.unit


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_wikimem_memdir_recaller_reads_extensions_and_returns_document_units(
    tmp_path, scope
) -> None:
    memory_dir = tmp_path / "mem"
    _write(
        memory_dir / "feedback" / "release.md",
        "---\n"
        "description: Team release database policy\n"
        "type: feedback\n"
        "---\n"
        "Use shared staging DB.\n",
    )
    parsed = ParsedQuery(
        raw="how should release database checks run",
        rewritten="how should release database checks run",
        extensions={
            "wikimem.memory_dirs": json.dumps(
                [{"scope": "team", "path": str(memory_dir)}]
            )
        },
    )
    recaller = WikimemMemdirRecaller()

    results = recaller.recall(scope, parsed, 5)

    assert recaller.channel() == RecallChannel.DOCUMENT
    assert len(results) == 1
    assert results[0].channel == RecallChannel.DOCUMENT
    assert results[0].unit_id == memory_file_unit_id(
        "team", str(memory_dir / "feedback" / "release.md")
    )
    assert results[0].score > 0


def test_wikimem_memdir_recaller_without_memory_dirs_returns_empty(scope) -> None:
    recaller = WikimemMemdirRecaller()

    assert recaller.recall(scope, ParsedQuery(raw="anything"), 5) == []
