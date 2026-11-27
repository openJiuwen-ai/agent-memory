"""Reader scans expose stable item ids and do not expose excluded role files."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jiuwen_memory_entry.local.sync.access import SourceAccess
from jiuwen_memory_entry.local.sync.grant import new_grant
from jiuwen_memory_entry.local.sync.model import RangeKey
from jiuwen_memory_entry.local.sync.readers.hermes import HermesReader
from jiuwen_memory_entry.local.sync.readers.workbuddy import WorkBuddyReader

pytestmark = pytest.mark.unit

_HERMES_FILES = (
    ("memories", "MEMORY.md"),
    ("profiles/work/memories", "MEMORY.md"),
    ("profiles/work/memories", "USER.md"),
    ("profiles/life/memories", "USER.md"),
)


def _access(root: Path, types: set[str]) -> SourceAccess:
    return SourceAccess(new_grant([str(root)], types=types, target_space="init"))


def _hermes_tree(tmp_path) -> Path:
    home = tmp_path / "hermes"
    for relative, name in _HERMES_FILES:
        path = home / relative / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"entry kept in {path.parent}\n", encoding="utf-8")
    return home


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


def test_hermes_reader_accepts_memories_dir_as_root(tmp_path):
    home = _hermes_tree(tmp_path)
    work = home / "profiles" / "work" / "memories"
    (work / "state.db").write_text("not a database", encoding="utf-8")
    types = {"fact", "profile", "session_turn"}
    for root, expected, kinds in (
        (work, {"work:MEMORY", "work:USER"}, {"fact", "profile"}),
        (home / "memories", {"default:MEMORY"}, {"fact"}),
    ):
        access = _access(root, types)
        result = HermesReader().scan(
            access, types=frozenset(types), since=None, limit=None
        )
        collections = {item.item_id.collection for item in result.items}
        assert collections == expected, (
            f"a memories root must only yield its own profile: {sorted(collections)}"
        )
        assert {item.root for item in result.items} == {str(root)}, (
            "items are attributed to the memories root itself"
        )
        assert set(result.complete) == {RangeKey(str(root), kind) for kind in kinds}, (
            f"only the files present were scanned, no session database: {result.complete}"
        )
        assert all(result.complete.values()), f"every file was read in full: {result.complete}"
        assert str(root / "state.db") not in access.opened, (
            "a memories root must never open a session database"
        )


def test_hermes_reader_scans_every_profile_from_home_root(tmp_path):
    home = _hermes_tree(tmp_path)
    database = sqlite3.connect(home / "state.db")
    database.execute("CREATE TABLE messages (role TEXT, content TEXT, created_at REAL)")
    database.commit()
    database.close()
    types = {"fact", "profile", "session_turn"}
    access = _access(home, types)
    result = HermesReader().scan(
        access, types=frozenset(types), since=None, limit=None
    )
    collections = {item.item_id.collection for item in result.items}
    assert collections == {"default:MEMORY", "work:MEMORY", "work:USER", "life:USER"}, (
        f"a hand-granted Hermes home still covers the default and every profile: "
        f"{sorted(collections)}"
    )
    assert result.complete[RangeKey(str(home), "session_turn")] is True, (
        "a Hermes home root still reads its own session database"
    )
    assert str(home / "state.db") in access.opened, "sessions were not read from the home"


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
