"""Startup reconcile (D7): downtime edits reach the shadow index before events open."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import asyncio

import pytest

from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
    WhitespaceTokenizer,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import SqliteDocumentShadowIndex
from jiuwen_memory.storage.watchdog_impl.local_watchdog import LocalWatchdog

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme")
DAILY = "memory/p1/daily_memory/2026-01-01.md"
MEMORY = "memory/p1/MEMORY.md"
USER = "USER.md"


def _watchdog(root) -> LocalWatchdog:
    return LocalWatchdog(
        shadow=SqliteDocumentShadowIndex(
            db_path=str(root / "shadow.db"), tokenizer=WhitespaceTokenizer()
        ),
        markdown_root=str(root),
        scope=SCOPE,
    )


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_start_reconciles_added_changed_deleted_and_skips_unchanged(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(root / DAILY, "# 2026-01-01\n第一行\n")
    _write(root / MEMORY, "# MEMORY\n旧事实\n")
    _write(root / USER, "# USER\n用户名 a\n")
    _write(root / "notes.md", "# notes\n不是记忆文件\n")
    wd = _watchdog(root)
    for rel in (DAILY, MEMORY, USER):
        wd._sync_one(rel, False)
    daily_before = wd._shadow.list_units_by_md(SCOPE, DAILY)
    assert daily_before, "fixture sync did not seed the shadow index"

    # Downtime edits: one modified, one deleted, one added; notes/daily untouched.
    _write(root / MEMORY, "# MEMORY\n新事实\n")
    (root / USER).unlink()
    added = root / "memory/p2/MEMORY.md"
    _write(added, "# MEMORY\n项目二\n")

    loop = asyncio.new_event_loop()
    calls: list[tuple[str, bool]] = []
    try:
        wd._loop = loop
        original = wd._sync_one

        def spy(name: str, deleted: bool = False) -> None:
            calls.append((name, deleted))
            original(name, deleted)

        wd._sync_one = spy  # type: ignore[method-assign]
        wd.start()
    finally:
        wd.stop()
        loop.close()

    assert len(calls) == 3, f"unchanged files were reconciled: {calls}"
    assert set(calls) == {
        ("memory/p1/MEMORY.md", False),
        ("memory/p2/MEMORY.md", False),
        ("USER.md", True),
    }, calls
    assert wd._shadow.list_units_by_md(SCOPE, DAILY) == daily_before, "unchanged file rewritten"
    assert wd._shadow.list_units_by_md(SCOPE, USER) == [], "deleted file left stale rows"
    assert not wd._has_drift(MEMORY), "modified file still diverges from the index"
    assert not wd._has_drift("memory/p2/MEMORY.md"), "added file missing from the index"


def test_reconcile_is_idempotent(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(root / USER, "# USER\n名\n")
    wd = _watchdog(root)
    assert wd.reconcile_all() == 1
    assert wd.reconcile_all() == 0, "second reconcile repeated an unchanged file"
    assert not wd._has_drift(USER)
