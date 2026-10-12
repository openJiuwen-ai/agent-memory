"""wikimem Markdown memory directory compatibility tests."""

from __future__ import annotations

import time

import pytest

from retrieval.wikimem_memdir import (
    WikimemDirectory,
    MemoryFileHeader,
    format_memory_manifest,
    load_memory_entrypoints,
    load_relevant_memory_files,
    normalize_memory_relative_path,
    scan_memory_directory,
    select_relevant_memory_files,
    should_include_memory_topic_file,
)

pytestmark = pytest.mark.unit


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_path_rules_match_wikimem_topic_file_boundaries() -> None:
    assert normalize_memory_relative_path("project/../feedback/testing.md") == "feedback/testing.md"
    assert normalize_memory_relative_path("../../outside.md") == "../../outside.md"

    assert should_include_memory_topic_file("feedback/testing.md")
    assert not should_include_memory_topic_file("MEMORY.md")
    assert not should_include_memory_topic_file("logs/2026/04/2026-04-02.md")
    assert not should_include_memory_topic_file("../../outside.md")
    assert not should_include_memory_topic_file("/absolute/outside.md")


def test_scan_memory_directory_parses_frontmatter_and_excludes_entrypoint(tmp_path) -> None:
    _write(
        tmp_path / "feedback" / "testing.md",
        "---\ndescription: \"Deploy: run staging gate before prod\"\ntype: feedback\n---\nBody.\n",
    )
    time.sleep(0.005)
    _write(
        tmp_path / "project" / "multiline.md",
        "---\n"
        "description: >\n"
        "  Release verification:\n"
        "  run smoke tests\n\n"
        "type: project\n"
        "---\n"
        "Body.\n",
    )
    _write(tmp_path / "MEMORY.md", "# entrypoint\n")
    _write(tmp_path / "logs" / "2026" / "04" / "2026-04-02.md", "# daily\n")

    headers = scan_memory_directory(tmp_path)

    assert [header.filename for header in headers] == [
        "project/multiline.md",
        "feedback/testing.md",
    ]
    assert headers[0].description == "Release verification: run smoke tests"
    assert headers[0].memory_type == "project"
    assert headers[1].description == "Deploy: run staging gate before prod"
    assert headers[1].memory_type == "feedback"


def test_manifest_and_header_selection_are_high_confidence() -> None:
    headers = [
        MemoryFileHeader(
            filename="feedback/testing.md",
            file_path="/mem/feedback/testing.md",
            mtime_ms=2000,
            description="Integration tests for database migration safety",
            memory_type="feedback",
        ),
        MemoryFileHeader(
            filename="reference/database-glossary.md",
            file_path="/mem/reference/database-glossary.md",
            mtime_ms=1000,
            description="Database glossary and terminology reference",
            memory_type="reference",
        ),
    ]

    manifest = format_memory_manifest(headers)
    selected = select_relevant_memory_files(
        "how should I test this database migration safely before release",
        headers,
        5,
    )

    assert "[feedback] feedback/testing.md" in manifest
    assert "1970-" in manifest
    assert selected == [headers[0]]


def test_load_relevant_memory_files_uses_body_fallback_and_team_dedupe(tmp_path) -> None:
    _write(
        tmp_path / "feedback" / "testing.md",
        "---\ndescription: Private testing preference\ntype: feedback\n---\nPrefer local checks.\n",
    )
    _write(
        tmp_path / "team" / "feedback" / "release.md",
        "---\n"
        "description: Team release policy\n"
        "type: feedback\n"
        "---\n"
        "Run shared staging database verification before release.\n",
    )

    files = load_relevant_memory_files(
        "how should we verify shared staging database behavior",
        [
            WikimemDirectory(scope="auto", path=str(tmp_path)),
            WikimemDirectory(scope="team", path=str(tmp_path / "team")),
        ],
        top_k=5,
        recent_tools=[],
        already_surfaced_file_paths=[],
    )

    assert len(files) == 1
    assert files[0].scope == "team"
    assert files[0].filename == "feedback/release.md"
    assert "shared staging database" in files[0].content


def test_load_memory_entrypoints_is_opt_in_truncated_and_secret_safe(tmp_path) -> None:
    _write(tmp_path / "MEMORY.md", "\n".join(f"line {i}" for i in range(250)))
    _write(tmp_path / "team" / "MEMORY.md", "github_pat_secret")

    entrypoints = load_memory_entrypoints(
        [
            WikimemDirectory(scope="auto", path=str(tmp_path)),
            WikimemDirectory(scope="team", path=str(tmp_path / "team")),
        ]
    )

    assert len(entrypoints) == 1
    assert entrypoints[0].scope == "auto"
    assert "line 199" in entrypoints[0].content
    assert "line 249" not in entrypoints[0].content
    assert "[truncated]" in entrypoints[0].content
