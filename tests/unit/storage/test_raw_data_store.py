from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment, Temporal
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.raw import KVRawDataStore
from jiuwen_memory.storage.security import StorageAccessContext, StorageAction, StorageSecurity
from jiuwen_memory.storage.storage_impl import CompositeStorage

pytestmark = pytest.mark.unit


def _unit(scope: Scope, unit_id: str, content: str, offset: int) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=scope,
        segments=[Segment(content=content)],
        temporal=Temporal(t_ingest=datetime.now(timezone.utc) + timedelta(seconds=offset)),
    )


class DenyRawSecurity(StorageSecurity):
    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        if resource == "raw":
            raise PermissionDeniedError("raw")


def test_raw_port_is_authorized_and_scope_isolated() -> None:
    scope = Scope(org="org", space="one", user="alice")
    other_scope = Scope(org="org", space="two", user="alice")
    storage = CompositeStorage(kv=InMemoryKVStore(), security=DenyRawSecurity())
    raw = storage.raw_port()
    unit = _unit(scope, "m1", "message", 0)

    with pytest.raises(PermissionDeniedError):
        raw.append_raw(scope, [unit])
    with pytest.raises(PermissionDeniedError):
        raw.list_raw(scope)
    with pytest.raises(PermissionDeniedError):
        raw.delete_raw(scope, [unit.id])

    # A direct adapter remains usable for isolated adapter tests; the production
    # Storage port above is the authorization boundary.
    direct = KVRawDataStore(InMemoryKVStore())
    direct.append_raw(scope, [unit])
    assert [item.id for item in direct.list_raw(scope)] == [unit.id]
    assert direct.list_raw(other_scope) == []


def test_raw_port_retains_recent_records_and_retries_same_id_as_update() -> None:
    scope = Scope(org="org", space="space")
    raw = KVRawDataStore(InMemoryKVStore())
    old = _unit(scope, "old", "old", -2)
    newest = _unit(scope, "new", "new", 2)
    raw.append_raw(scope, [old, newest], retain_limit=1)

    assert [item.id for item in raw.list_raw(scope)] == [newest.id]

    replacement = _unit(scope, newest.id, "replacement", 3)
    raw.append_raw(scope, [replacement])
    assert raw.list_raw(scope)[0].content == "replacement"


def test_raw_usage_and_purge_do_not_require_decoding_legacy_values() -> None:
    scope = Scope(org="org", space="space")
    kv = InMemoryKVStore()
    raw = KVRawDataStore(kv)
    kv.insert(scope, "/messages/legacy", b"legacy-bytes")

    usage = raw.usage(scope)
    assert usage.message_count == 1
    assert usage.storage_bytes == len(b"legacy-bytes")

    purged = raw.purge(scope)
    assert purged == usage
    assert kv.scan(scope) == []
