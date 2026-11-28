"""Space locks, source recovery, and manual resolution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory.common.security import internal_context
from jiuwen_memory_entry.local.sync.engine import SyncEngine
from jiuwen_memory_entry.local.sync.grant import new_grant
from jiuwen_memory_entry.local.sync.ledger import PendingCleanup, SyncLedger
from jiuwen_memory_entry.local.sync.model import (
    ImportItem,
    ItemId,
    RangeKey,
    ScanResult,
)
from jiuwen_memory_entry.local.sync.space import space_lock
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


class API:
    def __init__(self) -> None:
        self.units: dict[str, object] = {}
        self.deleted: list[str] = []

    def add(self, text, scope, **kwargs):
        unit = SimpleNamespace(
            id=f"u{len(self.units) + 1}",
            text=text,
            content=text,
            system_metadata={"sync_created_by": kwargs["operation_id"]},
        )
        self.units[unit.id] = unit
        return [unit]

    def get(self, unit_id, scope, **kwargs):
        return self.units.get(unit_id)

    @staticmethod
    def supports_conditional_delete():
        return True

    def delete(self, selector, *, security):
        self.deleted.extend(selector.unit_ids)
        return list(selector.unit_ids)

    @staticmethod
    def supports_operation_receipt():
        return True

    @staticmethod
    def operation_receipt(*args, **kwargs):
        return None

    @staticmethod
    def receipt_evicted(*args, **kwargs):
        return False


def _security():
    return internal_context(ScopedAuthenticator(Scope(org="acme", user="owner")))


class Reader:
    def __init__(self, item: ImportItem, root: str) -> None:
        self._item = item
        self._root = root

    def scan(self, access, *, types, since, limit):
        return ScanResult(
            items=[self._item],
            complete={RangeKey(self._root, "fact"): True},
        )


class Access:
    truncated = ()
    skipped = ()


def _install():
    return SimpleNamespace(
        install_id="install",
        host="hermes",
        actor=Scope(org="acme", user="owner"),
    )


def _sync_item(tmp_path, api):
    ledger = SyncLedger(tmp_path / "l.db")
    engine = SyncEngine(api, ledger)
    security = _security()
    grant = new_grant([str(tmp_path)], types={"fact"}, target_space="space")
    item = ImportItem(item_id=ItemId("fact", "c", "x"), text="hello", root=str(tmp_path))
    engine.run_scan(security, _install(), Reader(item, str(tmp_path)), Access(), grant)
    source = ledger.get_source("install", str(tmp_path), "fact|c|x", "space")
    assert source is not None
    key = f"{grant.grant_id}|space|{tmp_path}|fact|c|x"
    return ledger, engine, security, source, key


def test_space_lock_hashes_space_name(tmp_path):
    with space_lock("space-a", tmp_path):
        locks = list((tmp_path / "locks").glob("space-*.lock"))
        assert len(locks) == 1


def test_run_scan_records_output_and_resolve_abandons(tmp_path):
    ledger, engine, security, source, _key = _sync_item(tmp_path, API())
    output = ledger.get_output("u1")
    assert output is not None and output.origin == "sync"
    assert source.state == "ok"
    engine.resolve(security, source.op_id, "abandon")
    assert ledger.sources_by_op(source.op_id)[0].state == "abandoned"
    assert ledger.get_output("u1").protected is True
    ledger.close()


def test_recover_space_deletes_pending_after_no_unconfirmed(tmp_path):
    api = API()
    ledger, engine, security, source, key = _sync_item(tmp_path, api)
    ledger.release_refs(key, ["u1"])
    ledger.put_pending(PendingCleanup("u1", "space", key, "discard"))
    report = engine.recover_space(security, "space", data_dir=tmp_path)
    assert "u1" in report.deleted
    assert api.deleted == ["u1"]
    ledger.close()


class NoGetAPI(API):
    def get(self, unit_id, scope, **kwargs):
        return None


class FailingDeleteAPI(API):
    def delete(self, selector, *, security):
        raise RuntimeError("delete rejected")


def _written_source(tmp_path, api):
    ledger, engine, security, source, key = _sync_item(tmp_path, api)
    ledger.release_refs(key, list(source.refs))
    return ledger, engine, security


def test_delete_outputs_completes_when_unit_is_already_gone(tmp_path):
    api = NoGetAPI()
    ledger, engine, security = _written_source(tmp_path, api)
    source = ledger.sources_for_install("install")[0]
    key = f"{source.grant_id}|{source.target_space}|{source.root}|{source.item_id}"
    ledger.put_pending(PendingCleanup("u1", "space", key, "discard"))
    # An unreadable unit is already absent: cleanup is complete, so no ordinary
    # delete is issued and the pending row is cleared instead of protected.
    deleted, retained = engine.delete_outputs(security, ["u1"], space="space")
    assert deleted == ["u1"]
    assert retained == []
    assert api.deleted == []
    assert ledger.pending_for_space("space") == []
    ledger.close()


def test_delete_outputs_retains_when_conditional_delete_fails(tmp_path):
    api = FailingDeleteAPI()
    ledger, engine, security = _written_source(tmp_path, api)
    api.units["u1"] = SimpleNamespace(
        content="hello",
        system_metadata={"sync_created_by": ledger.get_output("u1").created_by_op},
    )
    deleted, retained = engine.delete_outputs(security, ["u1"], space="space")
    assert deleted == []
    assert retained == ["u1"]
    # A delete failure is not proof that the unit is unsafe to remove later;
    # keep it pending rather than permanently protecting it.
    assert ledger.get_output("u1").protected is False
    assert ledger.get_output("u1").retained_reason == ""
    ledger.close()


def test_pending_cleanup_survives_reopen_and_is_recovered(tmp_path):
    api = API()
    ledger, engine, security = _written_source(tmp_path, api)
    source = ledger.sources_for_install("install")[0]
    key = f"{source.grant_id}|{source.target_space}|{source.root}|{source.item_id}"
    ledger.put_pending(PendingCleanup("u1", "space", key, "discard"))
    ledger.close()

    reopened = SyncLedger(tmp_path / "l.db")
    engine = SyncEngine(api, reopened)
    report = engine.recover_space(security, "space", data_dir=tmp_path)
    assert "u1" in report.deleted
    assert api.deleted == ["u1"]
    reopened.close()


class ReceiptAfterFailureAPI(API):
    def __init__(self) -> None:
        super().__init__()
        self.receipt = None

    def add(self, text, scope, **kwargs):
        unit = SimpleNamespace(
            id="u1", text=text,
            system_metadata={"sync_created_by": kwargs["operation_id"]},
        )
        self.units["u1"] = unit
        self.receipt = SimpleNamespace(outputs=[{"unit_id": "u1"}])
        raise RuntimeError("commit acknowledgement lost")

    def operation_receipt(self, *args, **kwargs):
        return self.receipt


def test_scan_uses_receipt_after_lost_acknowledgement(tmp_path):
    ledger, _engine, _security_ctx, source, _key = _sync_item(
        tmp_path, ReceiptAfterFailureAPI()
    )
    assert source.state == "ok"
    assert source.refs == ["u1"]
    assert ledger.get_output("u1") is not None
    ledger.close()


class FailingAddAPI(API):
    def add(self, text, scope, **kwargs):
        raise RuntimeError("write failed before commit")


def test_scan_marks_unconfirmed_without_receipt(tmp_path):
    ledger = SyncLedger(tmp_path / "l.db")
    engine = SyncEngine(FailingAddAPI(), ledger)
    security = _security()
    grant = new_grant([str(tmp_path)], types={"fact"}, target_space="space")
    item = ImportItem(item_id=ItemId("fact", "c", "x"), text="hello", root=str(tmp_path))
    report = engine.run_scan(
        security, _install(), Reader(item, str(tmp_path)), Access(), grant
    )
    assert report.failed
    stored = ledger.get_source("install", str(tmp_path), "fact|c|x", "space")
    assert stored is not None and stored.state == "unconfirmed"
    assert stored.refs == []
    assert ledger.outputs_by_op(stored.op_id) == []
    ledger.close()


def test_replace_adopt_cleanup_old_queues_old_output(tmp_path):
    from jiuwen_memory_entry.local.sync.ledger import OutputRow, SourceRow

    ledger = SyncLedger(tmp_path / "l.db")
    ledger.put_output(OutputRow(
        "old", "space", "sync", "old-op", "old-digest", refcount=1
    ))
    api = ReceiptAfterFailureAPI()
    engine = SyncEngine(api, ledger)
    source = SourceRow(
        "install", str(tmp_path), "fact|c|x", "space", "digest",
        op_id="replace-op", intent="replace", old_refs=["old"], state="writing",
    )
    ledger.put_source(source)
    changed = engine.resolve(
        _security(), "replace-op", "adopt", replace_choice="cleanup_old"
    )
    assert changed == ["fact|c|x"]
    assert ledger.sources_by_op("replace-op")[0].state == "cleanup_pending"
    pending = ledger.pending_for_space("space")
    assert [row.unit_id for row in pending] == ["old"]
    ledger.close()
