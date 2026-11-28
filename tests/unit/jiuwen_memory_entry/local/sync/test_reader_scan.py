"""Reader scans expose stable item ids and do not expose excluded role files."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwen_memory_entry.local.sync.access import SourceAccess
from jiuwen_memory_entry.local.sync.grant import new_grant
from jiuwen_memory_entry.local.sync.readers.hermes import HermesReader
from jiuwen_memory_entry.local.sync.readers.workbuddy import WorkBuddyReader

pytestmark = pytest.mark.unit


def _access(root: Path, types: set[str]) -> SourceAccess:
    return SourceAccess(new_grant([str(root)], types=types, target_space="init"))


def test_memory_item_id_survives_insertion_and_reorder(tmp_path):
    root = tmp_path / "hermes"
    memories = root / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("# A\nfact a\n\n# B\nfact b\n", encoding="utf-8")
    grant_types = {"fact"}
    first = HermesReader().scan(
        _access(root, grant_types), types=frozenset(grant_types), since=None, limit=None
    )
    ids = {item.item_id.inner for item in first.items}
    (memories / "MEMORY.md").write_text(
        "# NEW\nnew\n\n# B\nfact b\n\n# A\nfact a\n", encoding="utf-8"
    )
    second = HermesReader().scan(
        _access(root, grant_types), types=frozenset(grant_types), since=None, limit=None
    )
    assert ids <= {item.item_id.inner for item in second.items}


def test_workbuddy_user_and_daily_same_text_are_distinct(tmp_path):
    root = tmp_path / "wb"
    (root / ".workbuddy" / "memory").mkdir(parents=True)
    (root / "USER.md").write_text("same fact", encoding="utf-8")
    (root / ".workbuddy" / "memory" / "2026-11-20.md").write_text(
        "same fact", encoding="utf-8"
    )
    (root / "SOUL.md").write_text("role", encoding="utf-8")
    types = {"profile", "daily_log"}
    result = WorkBuddyReader().scan(
        _access(root, types), types=frozenset(types), since=None, limit=None
    )
    assert len(result.items) == 2
    assert str(result.items[0].item_id).split("|")[0] != str(result.items[1].item_id).split("|")[0]
    assert all("SOUL" not in str(item.item_id) for item in result.items)
