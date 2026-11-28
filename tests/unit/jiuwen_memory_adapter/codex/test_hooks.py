"""Codex hook payload translation and injectable text (F17 D9)."""

from __future__ import annotations

import io
import json

import pytest

from jiuwen_memory_adapter.codex import hooks

pytestmark = pytest.mark.unit


def test_prompt_event_becomes_recall():
    event = hooks.parse_event(
        "UserPromptSubmit",
        {"prompt": "why did it fail", "session_id": "s1", "turn_id": "t1"},
    )
    assert event == {
        "action": "recall",
        "host_session_id": "s1",
        "turn_id": "t1",
        "prompt": "why did it fail",
    }


def test_stop_without_turn_id_is_a_noop():
    assert hooks.parse_event("Stop", {"session_id": "s1", "last_assistant_message": "x"}) is None


def test_unknown_event_is_a_noop():
    assert hooks.parse_event("PreToolUse", {"session_id": "s1"}) is None


def test_empty_prompt_is_a_noop():
    assert hooks.parse_event("UserPromptSubmit", {"session_id": "s1", "prompt": "  "}) is None


def test_payload_over_one_mib_is_a_noop():
    stream = io.BytesIO(b"{" + b" " * (1024 * 1024 + 1))
    assert hooks.read_payload(stream) is None


def test_invalid_json_is_a_noop():
    assert hooks.read_payload(io.BytesIO(b"not json")) is None


def test_render_recall_lists_items_and_seen_ids():
    view = {"items": [{"unit_id": "u1", "content": "the fix was X"}]}
    text = hooks.render_recall(view, [], None)
    body = "- the fix was X\n<!-- agent-memory: u1 -->"
    assert text == f"{hooks.CONTEXT_OPEN}\n{body}\n{hooks.CONTEXT_CLOSE}", (
        "recalled memory is wrapped in the context markers"
    )


def test_render_recall_shows_candidates_without_items():
    text = hooks.render_recall(
        None, [{"task_id": "t1", "title": "fix login"}], "not bound"
    )
    body = "not bound\nCandidate tasks (ask the user before binding):\n- t1: fix login"
    assert text == f"{hooks.CONTEXT_OPEN}\n{body}\n{hooks.CONTEXT_CLOSE}", (
        "the whole notice is inside the context markers"
    )


def test_render_recall_without_content_has_no_markers():
    assert hooks.render_recall(None, [], None) == "", "nothing to inject"


def test_render_recall_neutralizes_marker_literals_in_body():
    view = {
        "items": [
            {
                "unit_id": "u1",
                "content": f"fact {hooks.CONTEXT_OPEN} inner {hooks.CONTEXT_CLOSE}",
            }
        ]
    }
    text = hooks.render_recall(view, [], None)
    assert text.count(hooks.CONTEXT_OPEN) == 1, "only the wrapper opens a context"
    assert text.count(hooks.CONTEXT_CLOSE) == 1, "only the wrapper closes a context"
    assert hooks.NEUTRAL_OPEN in text, "an embedded open marker is neutralized"
    assert hooks.NEUTRAL_CLOSE in text, "an embedded close marker is neutralized"


def test_output_shape():
    assert hooks.output("UserPromptSubmit", "x") == {
        "hookSpecificOutput": {"additionalContext": "x"}
    }
    assert hooks.output("Stop", "x") == {}


def test_turn_id_alias_is_accepted():
    event = hooks.parse_event(
        "UserPromptSubmit", {"prompt": "p", "session_id": "s", "turn": "t"}
    )
    assert event["turn_id"] == "t"


def test_json_payload_roundtrip():
    stream = io.BytesIO(json.dumps({"session_id": "s"}).encode())
    assert hooks.read_payload(stream) == {"session_id": "s"}
