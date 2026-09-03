"""CompositeStorage 领域接口、能力与安全边界。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from jiuwen_memory.common.errors import (
    PermissionDeniedError,
    UnsupportedStorageCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.type_def import (
    EntityBatchResult,
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntityStoreFilters,
    MemoryUnit,
    Scope,
    Segment,
    memory_key,
)
from jiuwen_memory.common.type_def.scope import space_id_from_scope
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.entity_store import EntityStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.security import StorageAccessContext, StorageAction, StorageSecurity
from jiuwen_memory.storage.storage import StorageCapability, StorageProducer
from jiuwen_memory.storage.storage_impl import CompositeStorage
from jiuwen_memory.storage.types import IndexRemoveMode, KVMemoryListResult

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
        self.mget_batches: list[list[str]] = []

    def list(self, scope: Scope, **kwargs: Any) -> KVMemoryListResult:
        self.list_extensions = kwargs.get("extensions")
        return super().list(scope, **kwargs)

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        self.mget_batches.append(list(keys))
        return super().mget(scope, keys)


class ScopedEntityStoreDouble(EntityStore):
    """一体化 Storage 可注入的 EntityStore：按完整 Scope 做物理隔离。"""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str, str, str], dict[str, EntityRecord]] = {}

    @staticmethod
    def _key(scope: Scope) -> tuple[str, str, str, str, str]:
        return (scope.org, scope.space, scope.user, scope.agent, scope.session)

    def _bucket(self, scope: Scope) -> dict[str, EntityRecord]:
        return self.records.setdefault(self._key(scope), {})

    def store_type(self) -> StoreType:
        return StoreType.ENTITY

    def health(self) -> None:
        return None

    def ensure_index(self) -> None:
        return None

    def find_by_entity_text_hash(
        self,
        scope: Scope,
        entity_text_hashes: tuple[str, ...],
        *,
        limit: int = 500,
    ) -> list[EntityRecord]:
        hashes = set(entity_text_hashes)
        return [
            record
            for record in self._bucket(scope).values()
            if record.entity_text_hash in hashes
        ][:limit]

    def find_by_linked_memory_id(self, scope: Scope, memory_id: str) -> list[EntityRecord]:
        return [
            record
            for record in self._bucket(scope).values()
            if memory_id in record.linked_memory_ids
        ]

    def execute_operations(
        self,
        scope: Scope,
        operations: list[EntityOperation],
    ) -> EntityBatchResult:
        bucket = self._bucket(scope)
        successful: list[str] = []
        failed: list[str] = []
        for operation in operations:
            try:
                if operation.type is EntityOpType.INSERT:
                    assert operation.record is not None
                    if operation.record.space_id != space_id_from_scope(scope):
                        raise ValueError("record namespace differs from scope")
                    if operation.record.filters != EntityStoreFilters.from_scope(scope):
                        raise ValueError("record filters differ from scope")
                    bucket[operation.record.id] = operation.record
                    successful.append(operation.record.id)
                elif operation.type is EntityOpType.LINK:
                    assert operation.record_id is not None
                    record = bucket[operation.record_id]
                    linked = tuple(
                        sorted(set(record.linked_memory_ids) | set(operation.link_memory_ids))
                    )
                    bucket[record.id] = EntityRecord(
                        id=record.id,
                        space_id=record.space_id,
                        entity_text=record.entity_text,
                        entity_type=record.entity_type,
                        linked_memory_ids=linked,
                        filters=record.filters,
                        entity_text_hash=record.entity_text_hash,
                    )
                    successful.append(operation.record_id)
                elif operation.type is EntityOpType.UNLINK_UPDATE:
                    assert operation.record is not None
                    bucket[operation.record.id] = operation.record
                    successful.append(operation.record.id)
                elif operation.type is EntityOpType.DELETE:
                    assert operation.record_id is not None
                    bucket.pop(operation.record_id, None)
                    successful.append(operation.record_id)
            except Exception:
                failed.append(
                    str(
                        operation.record_id
                        if operation.record_id is not None
                        else operation.record.id
                        if operation.record is not None
                        else "?"
                    )
                )
        return EntityBatchResult(successful_ids=successful, failed_ids=failed)


class DenyEntitySecurity(StorageSecurity):
    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        if resource == "entity":
            raise PermissionDeniedError(f"{resource}:{action.value}")


class DenyRawAdminSecurity(StorageSecurity):
    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        if resource == "raw" and action is StorageAction.ADMIN:
            raise PermissionDeniedError(f"{resource}:{action.value}")


class ScopeRecordingSecurity(StorageSecurity):
    """Reject a proxy call if it loses the caller's keyword Scope."""

    def __init__(self, expected_scope: Scope) -> None:
        self.expected_scope = expected_scope
        self.calls: list[tuple[StorageAccessContext | None, Scope, StorageAction, str]] = []

    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        self.calls.append((access, scope, action, resource))
        if scope != self.expected_scope:
            raise PermissionDeniedError("unexpected scope")


class DenyEntityActionSecurity(StorageSecurity):
    """Exercise the action-specific guard for Entity bulk operations."""

    def __init__(self, denied_action: StorageAction) -> None:
        self.denied_action = denied_action

    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        if resource == "entity" and action is self.denied_action:
            raise PermissionDeniedError(f"entity:{action.value}")


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


def test_soft_delete_is_noop_and_body_stays_readable() -> None:
    """SOFT 软删除：无检索索引可移除，CompositeStorage 空操作，本体 get/list 仍可读。"""
    scope = Scope(org="org", space="space", user="user")
    storage = CompositeStorage(kv=RecordingKVStore())
    unit = _unit(scope, "u1", "first")
    storage.add(scope, [unit])

    storage.delete(scope, ["u1"], mode=IndexRemoveMode.SOFT)

    assert storage.get(scope, ["u1"]) == [unit]
    assert storage.list(scope).count == 1

    storage.delete(scope, ["u1"], mode=IndexRemoveMode.HARD)
    assert storage.get(scope, ["u1"]) == []


def test_get_reads_truth_source_in_one_deduplicated_batch() -> None:
    scope = Scope(org="org")
    kv = RecordingKVStore()
    storage = CompositeStorage(kv=kv)
    storage.add(scope, [_unit(scope, "u1"), _unit(scope, "u2")])

    # 一次 mget 覆盖去重后的 key；返回按输入顺序展开，重复 id 各自返回。
    assert [unit.id for unit in storage.get(scope, ["u2", "u1", "u2"])] == ["u2", "u1", "u2"]
    assert kv.mget_batches == [[memory_key("u2"), memory_key("u1")]]

    # mget 任一 key 缺失即抛 NotFoundError，由 _get_units 回退逐条并跳过缺失。
    kv.mget_batches.clear()
    assert [unit.id for unit in storage.get(scope, ["u1", "missing"])] == ["u1"]
    assert kv.mget_batches == [[memory_key("u1"), memory_key("missing")]]


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


def test_scopes_merge_primary_and_independent_raw_ports_without_duplicates() -> None:
    main_kv = InMemoryKVStore()
    raw_kv = InMemoryKVStore()
    storage = CompositeStorage(kv=main_kv, raw=raw_kv)
    primary_scope = Scope(org="org", space="primary", user="alice")
    raw_scope = Scope(org="org", space="raw", user="alice")
    shared_scope = Scope(org="org", space="shared", user="alice")

    main_kv.insert(primary_scope, "/memory/primary", b"primary")
    main_kv.insert(shared_scope, "/memory/shared", b"shared")
    storage.raw_port().append_raw(
        raw_scope,
        [MemoryUnit(id="raw-1", scope=raw_scope, segments=[Segment(content="raw")])],
    )
    storage.raw_port().append_raw(
        shared_scope,
        [MemoryUnit(id="raw-2", scope=shared_scope, segments=[Segment(content="shared")])],
    )

    scopes = storage.scopes()
    scope_keys = {
        (scope.org, scope.space, scope.user, scope.agent, scope.session)
        for scope in scopes
    }
    assert scope_keys == {
        ("org", "primary", "alice", "", ""),
        ("org", "raw", "alice", "", ""),
        ("org", "shared", "alice", "", ""),
    }
    assert len(scopes) == 3


def test_scopes_use_raw_port_authorization_proxy() -> None:
    storage = CompositeStorage(
        kv=InMemoryKVStore(),
        raw=InMemoryKVStore(),
        security=DenyRawAdminSecurity(),
    )

    with pytest.raises(PermissionDeniedError):
        storage.scopes()


def test_raw_and_entity_ports_authorize_keyword_scope_and_access() -> None:
    scope = Scope(org="org", space="space", user="alice")
    access = StorageAccessContext(actor=Scope(org="org", user="operator"))

    raw_security = ScopeRecordingSecurity(scope)
    raw_storage = CompositeStorage(kv=InMemoryKVStore(), security=raw_security)
    raw_storage.raw_port().append_raw(
        scope=scope,
        units=[_unit(scope, "raw-1")],
        access=access,
    )
    assert raw_security.calls == [(access, scope, StorageAction.ADD, "raw")]

    entity_security = ScopeRecordingSecurity(scope)
    entity_storage = CompositeStorage(
        kv=InMemoryKVStore(),
        entity=ScopedEntityStoreDouble(),
        security=entity_security,
    )
    assert entity_storage.entity_port().find_by_entity_text_hash(
        scope=scope,
        entity_text_hashes=("hash-alice",),
        access=access,
    ) == []
    assert entity_security.calls == [(access, scope, StorageAction.SEARCH, "entity")]


def test_entity_port_bulk_operations_authorize_insert_and_delete_actions() -> None:
    scope = Scope(org="org", space="space", user="alice")
    record = EntityRecord(
        id="entity-1",
        space_id=space_id_from_scope(scope),
        entity_text="Alice",
        entity_type="PROPER",
        linked_memory_ids=("memory-1",),
        filters=EntityStoreFilters.from_scope(scope),
        entity_text_hash="hash-alice",
    )

    denied_add = CompositeStorage(
        kv=InMemoryKVStore(),
        entity=ScopedEntityStoreDouble(),
        security=DenyEntityActionSecurity(StorageAction.ADD),
    )
    with pytest.raises(PermissionDeniedError):
        denied_add.entity_port().execute_operations(
            scope,
            [EntityOperation(type=EntityOpType.INSERT, record=record)],
        )

    denied_delete = CompositeStorage(
        kv=InMemoryKVStore(),
        entity=ScopedEntityStoreDouble(),
        security=DenyEntityActionSecurity(StorageAction.DELETE),
    )
    with pytest.raises(PermissionDeniedError):
        denied_delete.entity_port().execute_operations(
            scope,
            [EntityOperation(type=EntityOpType.DELETE, record_id=record.id)],
        )


def test_entity_capability_port_enforces_five_dimensional_scope_and_security() -> None:
    scope = Scope(org="org", space="space", user="alice", agent="agent-a", session="s1")
    other_scope = Scope(
        org="org", space="space", user="bob", agent="agent-a", session="s1"
    )
    entity = ScopedEntityStoreDouble()
    storage = CompositeStorage(kv=InMemoryKVStore(), entity=entity)

    assert StorageCapability.ENTITY in storage.capabilities()
    assert storage.has_entity()
    assert storage.has_entity_port()

    record = EntityRecord(
        id="entity-1",
        space_id=space_id_from_scope(scope),
        entity_text="Alice",
        entity_type="PROPER",
        linked_memory_ids=("memory-1",),
        filters=EntityStoreFilters.from_scope(scope),
        entity_text_hash="hash-alice",
    )
    storage.entity_port().execute_operations(
        scope,
        [EntityOperation(type=EntityOpType.INSERT, record=record)],
    )

    assert storage.entity_port().find_by_entity_text_hash(
        scope, ("hash-alice",)
    ) == [record]
    assert storage.entity_port().find_by_entity_text_hash(
        other_scope, ("hash-alice",)
    ) == []

    denied = CompositeStorage(
        kv=InMemoryKVStore(), entity=ScopedEntityStoreDouble(), security=DenyEntitySecurity()
    )
    with pytest.raises(PermissionDeniedError):
        denied.entity_port().find_by_entity_text_hash(scope, ("hash-alice",))


@pytest.mark.parametrize("dimension", ("org", "space", "user", "agent", "session"))
def test_entity_port_negative_isolation_covers_each_scope_dimension(dimension: str) -> None:
    """实体端口不能把任一 Scope 维度当成通配符。"""
    scope = Scope(org="org", space="space", user="alice", agent="agent-a", session="s1")
    other_scope = replace(scope, **{dimension: f"other-{dimension}"})
    storage = CompositeStorage(kv=InMemoryKVStore(), entity=ScopedEntityStoreDouble())
    record = EntityRecord(
        id="entity-1",
        space_id=space_id_from_scope(scope),
        entity_text="Alice",
        entity_type="PROPER",
        linked_memory_ids=("memory-1",),
        filters=EntityStoreFilters.from_scope(scope),
        entity_text_hash="hash-alice",
    )

    storage.entity_port().execute_operations(
        scope, [EntityOperation(type=EntityOpType.INSERT, record=record)]
    )

    assert storage.entity_port().find_by_entity_text_hash(
        other_scope, ("hash-alice",)
    ) == []
    assert storage.entity_port().find_by_linked_memory_id(other_scope, "memory-1") == []


def test_entity_port_mutations_do_not_cross_scope_with_same_id_and_hash() -> None:
    """同一实体 id/hash 在不同完整 Scope 下独立存取、更新和删除。"""
    scope_a = Scope(org="org", space="space", user="alice", agent="agent-a", session="s1")
    scope_b = replace(scope_a, user="bob")
    entity = ScopedEntityStoreDouble()
    storage = CompositeStorage(kv=InMemoryKVStore(), entity=entity)

    def record(scope: Scope, memory_id: str) -> EntityRecord:
        return EntityRecord(
            id="same-entity-id",
            space_id=space_id_from_scope(scope),
            entity_text="Alice",
            entity_type="PROPER",
            linked_memory_ids=(memory_id,),
            filters=EntityStoreFilters.from_scope(scope),
            entity_text_hash="same-hash",
        )

    port = storage.entity_port()
    port.execute_operations(
        scope_a,
        [EntityOperation(type=EntityOpType.INSERT, record=record(scope_a, "memory-a"))],
    )
    port.execute_operations(
        scope_b,
        [EntityOperation(type=EntityOpType.INSERT, record=record(scope_b, "memory-b"))],
    )

    port.execute_operations(
        scope_a,
        [
            EntityOperation(
                type=EntityOpType.LINK,
                record_id="same-entity-id",
                link_memory_ids=("extra-a",),
            )
        ],
    )
    a_record = port.find_by_entity_text_hash(scope_a, ("same-hash",))[0]
    b_record = port.find_by_entity_text_hash(scope_b, ("same-hash",))[0]
    assert set(a_record.linked_memory_ids) == {"memory-a", "extra-a"}
    assert b_record.linked_memory_ids == ("memory-b",)

    port.execute_operations(
        scope_a, [EntityOperation(type=EntityOpType.DELETE, record_id="same-entity-id")]
    )
    assert port.find_by_entity_text_hash(scope_a, ("same-hash",)) == []
    assert port.find_by_entity_text_hash(scope_b, ("same-hash",)) == [b_record]


def test_entity_named_port_is_available_without_default_and_remains_authorized() -> None:
    entity = ScopedEntityStoreDouble()
    storage = CompositeStorage(
        kv=InMemoryKVStore(), entity_ports={"alternate": entity}
    )
    scope = Scope(org="org", space="space", user="alice")

    assert not storage.has_entity_port()
    assert storage.has_entity_port("alternate")
    assert StorageCapability.ENTITY in storage.capabilities()
    with pytest.raises(UnsupportedStorageCapabilityError):
        storage.entity_port()

    record = EntityRecord(
        id="alternate-entity",
        space_id=space_id_from_scope(scope),
        entity_text="Alice",
        entity_type="PROPER",
        linked_memory_ids=("memory-1",),
        filters=EntityStoreFilters.from_scope(scope),
        entity_text_hash="hash-alice",
    )
    storage.entity_port("alternate").execute_operations(
        scope, [EntityOperation(type=EntityOpType.INSERT, record=record)]
    )
    assert storage.entity_port("alternate").find_by_entity_text_hash(
        scope, ("hash-alice",)
    ) == [record]


def test_entity_port_authorizes_reads_and_writes() -> None:
    scope = Scope(org="org", space="space", user="alice")
    denied = CompositeStorage(
        kv=InMemoryKVStore(), entity=ScopedEntityStoreDouble(), security=DenyEntitySecurity()
    )
    operation = EntityOperation(type=EntityOpType.DELETE, record_id="entity-1")

    with pytest.raises(PermissionDeniedError):
        denied.entity_port().find_by_linked_memory_id(scope, "memory-1")
    with pytest.raises(PermissionDeniedError):
        denied.entity_port().execute_operations(scope, [operation])


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
