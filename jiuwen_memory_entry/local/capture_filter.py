# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""What a bound turn writes (F17 D9): strip recalled memory, truncate long answers.

Injected recall text must never be written back — otherwise every recall amplifies
into the next write. Host-injected system content and thinking traces never reach
this function; adapters only pass the user text, the final answer and tool notes.
"""

from __future__ import annotations

from collections.abc import Sequence

ANSWER_BYTE_LIMIT = 8192
TRUNCATION_MARKER = "\n[已截断]"


def _truncate_utf8(text: str, limit: int) -> str:
    """Cut at a character boundary so multi-byte characters are never split."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip() + TRUNCATION_MARKER


def build_turn_content(
    user_text: str,
    assistant_text: str,
    *,
    injected: Sequence[str] = (),
    max_answer_bytes: int = ANSWER_BYTE_LIMIT,
) -> str:
    """One turn as ``user: …\\nassistant: …``, with recalled text removed."""
    cleaned = assistant_text or ""
    for text in injected:
        if text:
            cleaned = cleaned.replace(text, "")
    cleaned = cleaned.strip()
    if not cleaned:
        cleaned = "(empty answer)"
    cleaned = _truncate_utf8(cleaned, max_answer_bytes)
    return f"user: {user_text.strip()}\nassistant: {cleaned}"
