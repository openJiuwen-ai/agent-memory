"""CompositeStorage 领域接口、能力与安全边界。"""

from __future__ import annotations

from typing import Any

import pytest

from common.errors import PermissionDeniedError, UnsupportedStorageCapabilityError, ValidationError
from common.type_def import MemoryUnit, Scope, Segment
from config import AssemblyContext
from storage.bootstrap import register_backends
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from storage.security import StorageAccessContext, StorageAction, StorageSecurity
from storage.storage import StorageCapability, StorageProducer
from storage.storage_impl import CompositeStorage
from storage.types import KVMemoryListResult

pytestmark = pytest.mark.unit


class DenyWritesSecurity(StorageSecurity):
    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        if action in {StorageAction.ADD, StorageAction.UPDATE, StorageAction.DELETE}:
            raise PermissionDeniedError(action.value)


class RecordingKVStore(InMemoryKVStore):
    def __init__(self) -> None:
        super().__init__()
        self.list_extensions: dict[str, str] | None = None

    def list(self, scope: Scope, **kwargs: Any) -> KVMemoryListResult:
        self.list_extensions = kwargs.get("extensions")
        return super().list(scope, **kwargs)


def _unit(scope: Scope, unit_id: str, content: str = "content") -> MemoryUnit:
    return MemoryUnit(id=unit_id, scope=scope, segments=[Segment(content=content)])


def test_capabilities_and_ports_have_one_source_of_truth() -> None:
    kv = InMemoryKVStore()
    storage = CompositeStorage(kv=kv)

    assert storage.capabilities() == frozenset({StorageCapability.KV})
    assert storage.has_kv()
    assert not storage.has_vector()
    assert storage.kv.store_type() == kv.store_type()
    assert not storage.kv.security.enabled()
    with pytest.raises(UnsupportedStorageCapabilityError):
        _ = storage.vector


def test_memory_unit_crud_and_list_preserve_scope_and_count() -> None:
    scope = Scope(org="org", space="space", user="user")
    kv = RecordingKVStore()
    storage = CompositeStorage(kv=kv)
    first = _unit(scope, "u1", "first")
    second = _unit(scope, "u2", "second")

    storage.add(scope, [first, second])
    assert [unit.id for unit in storage.get(scope, ["u2", "missing", "u1"])] == ["u2", "u1"]
    assert [unit.id for unit in storage.get(scope, ["u1", "u1"])] == ["u1", "u1"]

    page = storage.list(scope, offset=1, limit=1, extensions={"route": "custom"})
    assert page.count == 2
    assert len(page.items) == 1
    assert kv.list_extensions == {"route": "custom"}

    updated = _unit(scope, "u1", "updated")
    storage.update(scope, [updated])
    assert storage.get(scope, ["u1"])[0].content == "updated"

    storage.delete(scope, ["u1"])
    assert storage.get(scope, ["u1"]) == []


def test_add_rejects_unit_owned_by_another_scope() -> None:
    requested = Scope(org="org", space="one")
    other = Scope(org="org", space="two")
    storage = CompositeStorage(kv=InMemoryKVStore())

    with pytest.raises(ValidationError):
        storage.add(requested, [_unit(other, "u1")])


def test_common_security_guards_domain_and_direct_port_operations() -> None:
    scope = Scope(org="org")
    storage = CompositeStorage(kv=InMemoryKVStore(), security=DenyWritesSecurity())

    with pytest.raises(PermissionDeniedError):
        storage.add(scope, [_unit(scope, "u1")])
    with pytest.raises(PermissionDeniedError):
        storage.kv.insert(scope, "/raw", b"value")

    assert storage.get(scope, ["missing"]) == []


def test_health_checks_storage_security_and_declared_store() -> None:
    storage = CompositeStorage(kv=InMemoryKVStore())

    assert storage.health() is None


def test_storage_producer_builds_named_composite_with_configured_ports() -> None:
    register_backends()
    context = AssemblyContext.from_dict(
        {
            "kv_store": {"truth": "memory"},
            "vector_store": {"semantic": "memory"},
            "storage": {
                "main": {
                    "target": "composite",
                    "params": {"kv_store": "truth", "vector_store": "semantic"},
                }
            },
        }
    )

    storage = StorageProducer.build_named("main", context)

    assert isinstance(storage, CompositeStorage)
    assert storage.capabilities() == frozenset(
        {StorageCapability.KV, StorageCapability.VECTOR}
    )
    assert StorageProducer.build_named("main", context) is storage


def test_storage_producer_rejects_unknown_retrieval_pipeline() -> None:
    register_backends()

    with pytest.raises(ValidationError, match="preferred_retrieval_pipeline"):
        StorageProducer.build(
            "composite",
            {"preferred_retrieval_pipeline": "unknown"},
            AssemblyContext(),
        )
