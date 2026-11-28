"""Capture filter: strip recalled memory, truncate answers at character boundaries."""

from __future__ import annotations

import pytest

from jiuwen_memory_entry.local.capture_filter import (
    ANSWER_BYTE_LIMIT,
    TRUNCATION_MARKER,
    build_turn_content,
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
