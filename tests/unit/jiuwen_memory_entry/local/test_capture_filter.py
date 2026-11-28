"""Capture filter: strip recalled memory, truncate answers at character boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwen_memory_entry.local.capture_filter import (
    ANSWER_BYTE_LIMIT,
    CONTEXT_CLOSE,
    CONTEXT_OPEN,
    TRUNCATION_MARKER,
    build_turn_content,
    strip_context_blocks,
)

pytestmark = pytest.mark.unit


def test_injected_recall_is_removed():
    content = build_turn_content(
        "question", "answer with RECALLED_TEXT inside", injected=["RECALLED_TEXT"]
    )
    assert "RECALLED_TEXT" not in content
    assert content.startswith("user: question\nassistant:")


def test_long_answer_truncated_without_splitting_characters():
    content = build_turn_content("q", "汉" * 5000)
    body = content.split("assistant: ", 1)[1]
    assert body.endswith(TRUNCATION_MARKER)
    text = body[: -len(TRUNCATION_MARKER)]
    assert len(text.encode("utf-8")) <= ANSWER_BYTE_LIMIT
    text.encode("utf-8").decode("utf-8")  # valid UTF-8 boundary


def test_empty_answer_marked():
    assert "(empty answer)" in build_turn_content("q", "   ", injected=["x"])


def test_strip_context_blocks():
    single = f"note\n{CONTEXT_OPEN}\n- recalled fact\n{CONTEXT_CLOSE}\nmore"
    assert strip_context_blocks(single) == "note\n\nmore", "a closed block is removed"
    double = f"{CONTEXT_OPEN}a{CONTEXT_CLOSE} keep {CONTEXT_OPEN}b{CONTEXT_CLOSE}"
    assert strip_context_blocks(double) == "keep", "every closed block is removed"
    unclosed = f"note\n{CONTEXT_OPEN}\n- recalled fact\n- more"
    assert strip_context_blocks(unclosed) == "note", "an unclosed block runs to the end"
    only_block = f"{CONTEXT_OPEN}x{CONTEXT_CLOSE}"
    assert strip_context_blocks(only_block) == "", "a block without text leaves nothing"
    assert strip_context_blocks("plain text") == "plain text", "unmarked text is kept"
    assert strip_context_blocks("  padded  ") == "padded", "the result is stripped"


def test_strip_removes_orphan_close_marker():
    text = f"kept {CONTEXT_CLOSE} text"
    assert strip_context_blocks(text) == "kept  text", (
        "a stray close marker is removed on its own"
    )
    doubled = f"a {CONTEXT_CLOSE} b {CONTEXT_CLOSE} c"
    assert strip_context_blocks(doubled) == "a  b  c", (
        "every orphan close marker is removed"
    )


def test_context_markers_are_identical_everywhere():
    from jiuwen_memory_adapter.codex import hooks as codex_hooks
    from jiuwen_memory_adapter.hermes import provider as hermes_provider

    assert codex_hooks.CONTEXT_OPEN == CONTEXT_OPEN, "codex open marker drifted"
    assert codex_hooks.CONTEXT_CLOSE == CONTEXT_CLOSE, "codex close marker drifted"
    assert hermes_provider.CONTEXT_OPEN == CONTEXT_OPEN, "hermes open marker drifted"
    assert hermes_provider.CONTEXT_CLOSE == CONTEXT_CLOSE, "hermes close marker drifted"
    source = (
        Path(codex_hooks.__file__).resolve().parents[1] / "deepseek_harness" / "index.js"
    ).read_text(encoding="utf-8")
    assert f'"{CONTEXT_OPEN}"' in source, "dsh open marker drifted"
    assert f'"{CONTEXT_CLOSE}"' in source, "dsh close marker drifted"


def test_context_block_in_answer_is_removed():
    answer = f"useful\n{CONTEXT_OPEN}\n- recalled fact\n{CONTEXT_CLOSE}\nstill useful"
    content = build_turn_content("q", answer, injected=["- recalled fact"])
    assert "recalled fact" not in content, "injected text is removed as before"
    assert CONTEXT_OPEN not in content and CONTEXT_CLOSE not in content, (
        "markers never reach a written turn"
    )
    assert content == "user: q\nassistant: useful\n\nstill useful"


def test_answer_that_is_only_a_context_block_is_empty():
    answer = f"{CONTEXT_OPEN}\n- recalled fact\n{CONTEXT_CLOSE}"
    assert "(empty answer)" in build_turn_content("q", answer), (
        "a bare block leaves no answer"
    )
