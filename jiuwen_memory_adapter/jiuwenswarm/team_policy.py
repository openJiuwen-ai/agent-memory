# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-side validation for host-confirmed Team conclusions."""
from __future__ import annotations

TEAM_LABELS = frozenset({"decision", "lesson", "member", "context"})


def _invalid_conclusion_reason(item: object) -> str:
    if not isinstance(item, dict):
        return "invalid_item"
    label, content = item.get("label"), item.get("content")
    if not isinstance(label, str) or label not in TEAM_LABELS:
        return "invalid_label"
    if not isinstance(content, str) or not content.strip():
        return "invalid_content"
    return ""


def partition_team_conclusions(
    items: list[dict[str, str]],
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """Validate the whole batch before writes; return safe rejection counts only."""
    valid: list[tuple[str, str]] = []
    skipped: dict[str, int] = {}
    for item in items:
        reason = _invalid_conclusion_reason(item)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            valid.append((item["label"], item["content"].strip()))
    return valid, skipped


def validate_team_binding(project: str, team: str) -> None:
    """Check host coordinate agreement; storage validates the canonical path format."""
    if not project or not isinstance(team, str) or team.rpartition("--")[0] != project:
        raise ValueError("Team key does not match the bound project")
