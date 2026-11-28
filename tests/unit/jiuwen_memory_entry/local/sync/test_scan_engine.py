"""The scan path writes through the API and only deletes complete ranges."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory_entry.local.sync.engine import SyncEngine
from jiuwen_memory_entry.local.sync.grant import new_grant
from jiuwen_memory_entry.local.sync.ledger import SyncLedger
from jiuwen_memory_entry.local.sync.model import ImportItem, ItemId, RangeKey, ScanResult

pytestmark = pytest.mark.unit


class API:
    def __init__(self):
        self.added = []
        self.deleted = []

    def add(self, text, scope, **kwargs):
        self.added.append(text)
        return [SimpleNamespace(id=f"u{len(self.added)}")]

    @staticmethod
    def batch_update(*args, **kwargs):
        raise AssertionError("unexpected update")

    def delete(self, selector, *, security):
        self.deleted.extend(selector.unit_ids)
        return list(selector.unit_ids)


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


def test_scan_writes_and_skips_repeat(tmp_path):
    ledger = SyncLedger(tmp_path / "l.db")
    api = API()
    grant = new_grant([str(tmp_path)], types={"fact"}, target_space="init")
    item = ImportItem(item_id=ItemId("fact", "MEMORY", "a"), text="hello")
    result = ScanResult(
        items=[item], complete={RangeKey(str(tmp_path), "fact"): True}
    )
    engine = SyncEngine(api, ledger)
    first = engine.run_scan(None, _install(), Reader(result), Access(), grant)
    second = engine.run_scan(None, _install(), Reader(result), Access(), grant)
    assert first.added == 1
    assert second.skipped == 1
    assert api.added == ["hello"]
    conn = getattr(ledger, "_conn")
    state = conn.execute("SELECT state, refs FROM sync_source").fetchone()
    assert state[0] == "ok"
    assert state[1] == '["u1"]'
    assert conn.execute("SELECT COUNT(*) FROM sync_output").fetchone()[0] == 1
    ledger.close()


def test_scan_does_not_delete_when_range_incomplete(tmp_path):
    ledger = SyncLedger(tmp_path / "l.db")
    api = API()
    grant = new_grant([str(tmp_path)], types={"fact"}, target_space="init")
    item = ImportItem(item_id=ItemId("fact", "MEMORY", "a"), text="hello")
    full = ScanResult(items=[item], complete={RangeKey(str(tmp_path), "fact"): True})
    empty = ScanResult(items=[], complete={RangeKey(str(tmp_path), "fact"): False})
    engine = SyncEngine(api, ledger)
    engine.run_scan(None, _install(), Reader(full), Access(), grant)
    report = engine.run_scan(None, _install(), Reader(empty), Access(), grant)
    assert report.deleted_in_source == []
    assert api.deleted == []
    ledger.close()


def test_scan_defers_delete_when_another_op_is_unconfirmed(tmp_path):
    ledger = SyncLedger(tmp_path / "l.db")
    api = API()
    grant = new_grant([str(tmp_path)], types={"fact"}, target_space="init")
    item = ImportItem(item_id=ItemId("fact", "MEMORY", "a"), text="hello")
    full = ScanResult(items=[item], complete={RangeKey(str(tmp_path), "fact"): True})
    engine = SyncEngine(api, ledger)
    engine.run_scan(None, _install(), Reader(full), Access(), grant)
    conn = getattr(ledger, "_conn")
    with conn:
        conn.execute("UPDATE sync_source SET state='writing'")
    second = ImportItem(item_id=ItemId("fact", "MEMORY", "b"), text="world")
    changed = ScanResult(
        items=[item, second], complete={RangeKey(str(tmp_path), "fact"): True}
    )
    report = engine.run_scan(None, _install(), Reader(changed), Access(), grant)
    assert report.deleted_in_source == []
    assert report.truncated == ["sync deferred: unconfirmed operation requires recovery"]
    assert report.writes == {}
    assert api.deleted == []
    ledger.close()


class SharedAPI(API):
    def add(self, text, scope, **kwargs):
        self.added.append(text)
        return [SimpleNamespace(
            id="shared", system_metadata={"sync_created_by": kwargs.get("operation_id")}
        )]


def test_scan_counts_references_for_deduped_output(tmp_path):
    ledger = SyncLedger(tmp_path / "l.db")
    api = SharedAPI()
    grant = new_grant([str(tmp_path)], types={"fact"}, target_space="init")
    items = [
        ImportItem(item_id=ItemId("fact", "MEMORY", "a"), text="same"),
        ImportItem(item_id=ItemId("fact", "MEMORY", "b"), text="same"),
    ]
    result = ScanResult(
        items=items, complete={RangeKey(str(tmp_path), "fact"): True}
    )
    engine = SyncEngine(api, ledger)
    report = engine.run_scan(None, _install(), Reader(result), Access(), grant)
    assert report.added == 2
    conn = getattr(ledger, "_conn")
    row = conn.execute("SELECT refcount FROM sync_output WHERE unit_id='shared'").fetchone()
    assert row[0] == 2
    ledger.close()


class ReceiptThenErrorAPI(API):
    def __init__(self):
        super().__init__()
        self.unit = None
        self.receipt = None

    def add(self, text, scope, **kwargs):
        self.unit = SimpleNamespace(
            id="u1",
            content=text,
            system_metadata={"sync_created_by": kwargs["operation_id"]},
        )
        self.receipt = SimpleNamespace(
            outputs=[{"unit_id": "u1", "created": True}]
        )
        raise RuntimeError("response lost after commit")

    def get(self, unit_id, scope, **kwargs):
        return self.unit

    def operation_receipt(self, *args, **kwargs):
        return self.receipt


def test_run_scan_recovers_committed_receipt_after_error(tmp_path):
    ledger = SyncLedger(tmp_path / "l.db")
    api = ReceiptThenErrorAPI()
    grant = new_grant([str(tmp_path)], types={"fact"}, target_space="init")
    item = ImportItem(item_id=ItemId("fact", "MEMORY", "a"), text="hello")
    result = ScanResult(
        items=[item], complete={RangeKey(str(tmp_path), "fact"): True}
    )
    report = SyncEngine(api, ledger).run_scan(
        None, _install(), Reader(result), Access(), grant
    )
    source = ledger.get_source("i", str(tmp_path), "fact|MEMORY|a", "init")
    assert report.failed == []
    assert report.added == 1
    assert source is not None and source.state == "ok" and source.refs == ["u1"]
    ledger.close()
