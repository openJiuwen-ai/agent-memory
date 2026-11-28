"""Ledger identity includes the source root, so one root cannot delete another."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory_entry.local.sync.engine import SyncEngine
from jiuwen_memory_entry.local.sync.grant import new_grant
from jiuwen_memory_entry.local.sync.ledger import SyncLedger
from jiuwen_memory_entry.local.sync.model import ImportItem, ItemId, RangeKey, ScanResult

pytestmark = pytest.mark.unit


class FakeAPI:
    def __init__(self):
        self.added = 0

    def add(self, text, scope, **kwargs):
        self.added += 1
        return [SimpleNamespace(id=f"u{self.added}")]

    @staticmethod
    def batch_update(*args, **kwargs):
        raise AssertionError("no update expected")

    @staticmethod
    def delete(*args, **kwargs):
        raise AssertionError("no delete expected")


class Reader:
    def __init__(self, result):
        self.result = result

    def scan(self, access, *, types, since, limit):
        return self.result


class Access:
    opened = []
    skipped = []
    truncated = []


def _install():
    return SimpleNamespace(
        install_id="i", host="hermes", actor=Scope(org="o", user="u")
    )


def _item(root, name="a"):
    return ImportItem(
        item_id=ItemId("fact", "MEMORY", name),
        text=f"hello {name}",
        root=str(root),
    )


def _result(root, items):
    return ScanResult(items=items, complete={RangeKey(str(root), "fact"): True})


def test_same_source_key_in_two_roots_is_two_entries(tmp_path):
    ledger = SyncLedger(tmp_path / "l.db")
    api = FakeAPI()
    engine = SyncEngine(api, ledger)
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    for root in (one, two):
        grant = new_grant([str(root)], types={"fact"}, target_space="init")
        report = engine.run_scan(
            None,
            _install(),
            Reader(_result(root, [_item(root)])),
            Access(),
            grant,
        )
        assert report.added == 1
    assert api.added == 2
    conn = getattr(ledger, "_conn")  # private ledger handle: no public row count
    assert conn.execute("SELECT COUNT(*) FROM sync_source").fetchone()[0] == 2
    ledger.close()


def test_deletion_is_scoped_to_the_scanned_root(tmp_path):
    ledger = SyncLedger(tmp_path / "l.db")
    api = FakeAPI()
    engine = SyncEngine(api, ledger)
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    first = new_grant(
        [str(one)], types={"fact"}, target_space="init", mirror_deletes=True
    )
    other = new_grant([str(two)], types={"fact"}, target_space="init")
    engine.run_scan(
        None,
        _install(),
        Reader(_result(one, [_item(one, "a"), _item(one, "b")])),
        Access(),
        first,
    )
    engine.run_scan(
        None, _install(), Reader(_result(two, [_item(two)])), Access(), other
    )
    report = engine.run_scan(
        None, _install(), Reader(_result(one, [_item(one, "a")])), Access(), first
    )
    # ``deleted_in_source`` holds ledger source keys, not file paths; the key
    # embeds the scanned root, so only root one's missing entry may appear.
    missing = f"{first.grant_id}|init|{one}|fact|MEMORY|b"
    assert report.deleted_in_source == [missing]
    assert report.deletes == {missing}
    ledger.close()
