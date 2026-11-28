# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Codex hook payload translation (F17 D9).

Codex invokes ``jiuwen-memory hook codex <event>`` as a short-lived process. This
module only parses payloads and renders output; all memory decisions belong to the
local service. Unknown or incomplete payloads are a clean no-op (exit 2) so a turn is
never interrupted.

Exit codes: 0 = output produced, 2 = deliberate no-op, 1 = handled error.
"""

from __future__ import annotations

import json

MAX_PAYLOAD_BYTES = 1024 * 1024
RECALL_EVENTS = {"UserPromptSubmit"}
CAPTURE_EVENTS = {"Stop"}
CONTEXT_OPEN = "<agent-memory-context>"
CONTEXT_CLOSE = "</agent-memory-context>"
NEUTRAL_OPEN = "‹agent-memory-context›"
NEUTRAL_CLOSE = "‹/agent-memory-context›"


def _neutralize_markers(text: str) -> str:
    """Keep recalled text from closing the wrapper or forging a new block."""
    return text.replace(CONTEXT_OPEN, NEUTRAL_OPEN).replace(
        CONTEXT_CLOSE, NEUTRAL_CLOSE
    )


def parse_event(event: str, payload: object) -> dict | None:
    """Normalize one hook payload; ``None`` means "nothing to do, exit 2"."""
    if not isinstance(payload, dict):
        return None
    if event in RECALL_EVENTS:
        prompt = payload.get("prompt")
        session_id = payload.get("session_id")
        if not isinstance(prompt, str) or not prompt.strip() or not isinstance(session_id, str):
            return None
        if not session_id:
            return None
        return {
            "action": "recall",
            "host_session_id": session_id,
            "turn_id": _turn_id(payload),
            "prompt": prompt,
        }
    if event in CAPTURE_EVENTS:
        answer = payload.get("last_assistant_message")
        session_id = payload.get("session_id")
        turn_id = _turn_id(payload)
        if not isinstance(answer, str) or not session_id or not turn_id:
            return None
        return {
            "action": "capture",
            "host_session_id": session_id,
            "turn_id": turn_id,
            "prompt": str(payload.get("prompt") or ""),
            "answer": answer,
        }
    return None


def _turn_id(payload: dict) -> str:
    value = payload.get("turn_id") or payload.get("turn")
    return value if isinstance(value, str) and value.strip() else ""


def render_recall(view: dict | None, candidates: list[dict], notice: str | None) -> str:
    """Injectable text for ``hookSpecificOutput.additionalContext``."""
    lines: list[str] = []
    if notice:
        lines.append(notice)
    items = (view or {}).get("items") or []
    for item in items:
        content = _neutralize_markers(str(item.get("content") or "").strip())
        if content:
            lines.append(f"- {content}")
    if candidates and not items:
        lines.append("Candidate tasks (ask the user before binding):")
        for candidate in candidates:
            title = candidate.get("title") or candidate.get("description") or candidate["task_id"]
            lines.append(f"- {candidate['task_id']}: {title}")
    if not lines:
        return ""
    seen = [str(item.get("unit_id", "")) for item in items if item.get("unit_id")]
    if seen:
        lines.append("<!-- agent-memory: " + ",".join(seen) + " -->")
    return f"{CONTEXT_OPEN}\n" + "\n".join(lines) + f"\n{CONTEXT_CLOSE}"


def output(event: str, text: str) -> dict:
    """Codex hook output object (stdout JSON)."""
    if event in RECALL_EVENTS:
        return {"hookSpecificOutput": {"additionalContext": text}}
    return {}


def read_payload(stream) -> object | None:
    """Read at most 1 MiB of JSON from stdin; oversized or invalid input is a no-op."""
    raw = stream.read(MAX_PAYLOAD_BYTES + 1)
    if not raw or len(raw) > MAX_PAYLOAD_BYTES:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
