"""The scan path writes through the API and only deletes complete ranges."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory_entry.local.capture_filter import CONTEXT_CLOSE, CONTEXT_OPEN
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
    def supports_operation_receipt():
        return False

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

    @staticmethod
    def supports_operation_receipt():
        return True

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

    @staticmethod
    def receipt_evicted(*args, **kwargs):
        return False


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


def test_sync_ignores_reflowed_context(tmp_path):
    ledger = SyncLedger(tmp_path / "l.db")
    api = API()
    grant = new_grant([str(tmp_path)], types={"fact"}, target_space="init")
    item = ImportItem(
        item_id=ItemId("fact", "MEMORY", "a"),
        text=f"{CONTEXT_OPEN}reflowed recall{CONTEXT_CLOSE}",
    )
    result = ScanResult(
        items=[item], complete={RangeKey(str(tmp_path), "fact"): True}
    )
    report = SyncEngine(api, ledger).run_scan(
        None, _install(), Reader(result), Access(), grant
    )
    assert report.added == 0, "reflowed context must not be written"
    assert report.skipped == 1, "the reflowed entry is reported as skipped"
    assert report.writes == {}, "no planned write for reflowed context"
    assert api.added == [], "add is never called for reflowed context"
    ledger.close()


def test_reflow_only_profile_entry_does_not_block_legacy_migration(tmp_path):
    ledger = SyncLedger(tmp_path / "l.db")
    api = API()
    root = str(tmp_path)
    grant = new_grant([root], types={"profile"}, target_space="init")
    item = ImportItem(
        item_id=ItemId("profile", "p:USER", "u1"),
        text=f"{CONTEXT_OPEN}reflowed recall{CONTEXT_CLOSE}",
        root=root,
    )
    legacy_key = f"{grant.grant_id}|{grant.target_space}|{root}|profile|p:USER|file"
    ledger.upsert("i", legacy_key, ["oldunit"], "olddigest", 0.0)
    result = ScanResult(items=[item], complete={RangeKey(root, "profile"): True})
    report = SyncEngine(api, ledger).run_scan(
        None, _install(), Reader(result), Access(), grant
    )
    assert report.skipped == 1, "the reflow-only entry is skipped"
    assert api.added == [], "nothing is written for a reflow-only entry"
    assert ledger.get("i", legacy_key) is None, (
        "a pure reflow entry must not block legacy migration"
    )
    pending = {row.unit_id for row in ledger.pending_for_space("init")}
    assert "oldunit" in pending, "the legacy output moves to pending cleanup"
    ledger.close()


class DedupAPI(API):
    def __init__(self, units):
        super().__init__()
        self.units = units
        self.synced = []

    def add(self, text, scope, **kwargs):
        self.synced.append(text)
        for unit in self.units.values():
            if unit.content == text:
                return [unit]
        unit = SimpleNamespace(
            id=f"u{len(self.units)}",
            content=text,
            system_metadata={"sync_created_by": kwargs["operation_id"]},
        )
        self.units[unit.id] = unit
        return [unit]

    def get(self, unit_id, scope, **kwargs):
        return self.units[unit_id]

    @staticmethod
    def supports_conditional_delete():
        return True


def _security():
    return SimpleNamespace(auth=SimpleNamespace(actor=Scope(org="o", user="u")))


def _source_key(grant, inner):
    return f"{grant.grant_id}|{grant.target_space}|{grant.roots[0]}|fact|MEMORY|{inner}"


def test_identical_content_from_other_source_is_reused(tmp_path):
    ledger = SyncLedger(tmp_path / "l.db")
    api = DedupAPI({
        "existing": SimpleNamespace(
            id="existing",
            content="same fact",
            system_metadata={"sync_created_by": "another-install"},
        )
    })
    grant = new_grant(
        [str(tmp_path)], types={"fact"}, target_space="init", mirror_deletes=True
    )
    engine = SyncEngine(api, ledger)
    shared = ImportItem(item_id=ItemId("fact", "MEMORY", "shared"), text="same fact")
    kept = ImportItem(item_id=ItemId("fact", "MEMORY", "kept"), text="kept fact")
    complete = {RangeKey(str(tmp_path), "fact"): True}
    full = ScanResult(items=[shared, kept], complete=complete)
    report = engine.run_scan(_security(), _install(), Reader(full), Access(), grant)
    assert report.added == 2, "both entries complete"
    assert sorted(api.units) == ["existing", "u1"], "identical text reuses the unit"
    output = ledger.get_output("existing")
    assert output is not None and output.origin == "external", "reuse is external"
    assert output.refcount == 1, "a reference is recorded, the unit is not owned"
    source = ledger.get_source("i", grant.roots[0], "fact|MEMORY|shared", "init")
    assert source is not None and source.refs == ["existing"], "reference only"

    only_kept = ScanResult(items=[kept], complete=complete)
    report = engine.run_scan(_security(), _install(), Reader(only_kept), Access(), grant)
    assert report.deletes == {_source_key(grant, "shared")}, "the source entry is gone"
    assert api.deleted == [], "a unit created elsewhere survives a mirror delete"
    retained = ledger.get_output("existing")
    assert retained is not None and retained.protected, "the external unit is kept"
    ledger.close()
