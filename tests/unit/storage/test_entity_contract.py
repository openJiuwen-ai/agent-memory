# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 S4：EntityStore 契约补齐（Scope / shards / 增量 UNLINK / 兼容适配器）。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import (
    UnsupportedStorageCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.entity import (
    EntityBatchResult,
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntityStoreFilters,
    hash_entity_text,
)
from jiuwen_memory.storage.capability import StoreCapability
from jiuwen_memory.storage.entity_impl.in_memory_entity_store import InMemoryEntityStore
from jiuwen_memory.storage.entity_impl.legacy_entity_store_adapter import (
    LegacyEntityStoreAdapter,
)
from jiuwen_memory.storage.shard_routing import RuleBasedShardResolver, ShardLocation

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")
FILTERS = EntityStoreFilters(actor_id="u1")
HASH = hash_entity_text("alice")


def record(record_id: str = "e1", linked: tuple[str, ...] = ("u1",)) -> EntityRecord:
    return EntityRecord(
        id=record_id,
        space_id="acme",
        entity_text="Alice",
        entity_type="PROPER",
        linked_memory_ids=linked,
        filters=FILTERS,
        entity_text_hash=HASH,
        embedding=[1.0, 0.0],
    )


def test_memory_store_unlink_is_incremental_and_keeps_empty_record() -> None:
    """TC-S4-4：UNLINK 从不删除记录；空记录不被查询返回。"""
    store = InMemoryEntityStore()
    store.execute_operations(
        SCOPE, [EntityOperation(type=EntityOpType.INSERT, record=record(linked=("u1", "u2")))]
    )
    result = store.execute_operations(
        SCOPE,
        [
            EntityOperation(
                type=EntityOpType.UNLINK, record_id="e1", unlink_memory_ids=("u1", "u2")
            )
        ],
    )
    assert result.successful_ids == ["e1"]
    assert store.find_by_entity_text_hash(SCOPE, (HASH,), filters=FILTERS) == []
    # 记录还在（未被删除），DELETE_IF_EMPTY 再次确认时删除
    kept = store.execute_operations(
        SCOPE, [EntityOperation(type=EntityOpType.DELETE_IF_EMPTY, record_id="e1")]
    )
    assert kept.successful_ids == ["e1"]


def test_memory_store_delete_if_empty_noop_on_non_empty() -> None:
    store = InMemoryEntityStore()
    store.execute_operations(
        SCOPE, [EntityOperation(type=EntityOpType.INSERT, record=record())]
    )
    store.execute_operations(
        SCOPE, [EntityOperation(type=EntityOpType.DELETE_IF_EMPTY, record_id="e1")]
    )
    assert store.find_by_entity_text_hash(SCOPE, (HASH,), filters=FILTERS)


def test_memory_store_unlink_missing_record_is_idempotent() -> None:
    """TC-S4：对不存在的记录 UNLINK 幂等成功（不报失败）。"""
    store = InMemoryEntityStore()
    result = store.execute_operations(
        SCOPE,
        [
            EntityOperation(
                type=EntityOpType.UNLINK, record_id="missing", unlink_memory_ids=("u1",)
            )
        ],
    )
    assert result.successful_ids == ["missing"]
    assert not result.failed_ids


def test_memory_store_shards_view() -> None:
    """TC-S4-2：关联按分片分组，shards 过滤只返回选中分组。"""
    store = InMemoryEntityStore()
    store.execute_operations(
        SCOPE,
        [
            EntityOperation(
                type=EntityOpType.INSERT,
                record=record(linked=("u1", "u2")),
                memory_shards={"u1": "profile", "u2": "working"},
            )
        ],
    )
    found = store.find_by_entity_text_hash(
        SCOPE, (HASH,), filters=FILTERS, shards=frozenset({"working"})
    )
    assert found[0].linked_memory_ids == ("u2",)
    assert store.find_by_entity_text_hash(
        SCOPE, (HASH,), filters=FILTERS, shards=frozenset({"main"})
    ) == []


def test_memory_store_filters_isolate_actor() -> None:
    store = InMemoryEntityStore()
    store.execute_operations(
        SCOPE, [EntityOperation(type=EntityOpType.INSERT, record=record())]
    )
    assert store.find_by_entity_text_hash(
        SCOPE, (HASH,), filters=EntityStoreFilters(actor_id="other")
    ) == []


def test_memory_store_search_scores_embedding() -> None:
    store = InMemoryEntityStore()
    store.execute_operations(
        SCOPE, [EntityOperation(type=EntityOpType.INSERT, record=record())]
    )
    hits = store.search(SCOPE, [1.0, 0.0], top_k=5, filters=FILTERS)
    assert [hit.record.id for hit in hits] == ["e1"]
    assert store.search(SCOPE, [0.0, 1.0], top_k=5, filters=FILTERS) == []


# ---------------------------------------------------------------------------
# 旧签名兼容适配器
# ---------------------------------------------------------------------------


class _LegacyStore:
    """旧签名实现：首参 space_id: str，只有 UNLINK_UPDATE / DELETE。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._records = {("acme", "e1"): record(linked=("u1", "u2"))}

    @staticmethod
    def store_type():
        return InMemoryEntityStore().store_type()

    @staticmethod
    def health() -> None:
        return None

    @staticmethod
    def ensure_index() -> None:
        return None

    def find_by_entity_text_hash(self, space_id, hashes, *, filters, limit=500):
        self.calls.append(("find", (space_id, hashes)))
        return list(self._records.values()) if space_id == "acme" else []

    def find_by_linked_memory_id(self, space_id, memory_id, *, filters):
        self.calls.append(("linked", (space_id, memory_id)))
        return [
            rec
            for rec in self._records.values()
            if memory_id in rec.linked_memory_ids
        ]

    def execute_operations(self, space_id, operations):
        self.calls.append(("ops", (space_id, tuple(op.type for op in operations))))
        successful: list[str] = []
        for op in operations:
            if op.type is EntityOpType.DELETE:
                self._records.pop((space_id, op.record_id), None)
                successful.append(op.record_id)
            elif op.type is EntityOpType.UNLINK_UPDATE:
                self._records[(space_id, op.record.id)] = op.record
                successful.append(op.record.id)
        return EntityBatchResult(successful_ids=successful, failed_ids=[])

    def search(self, space_id, query_vector, *, top_k, filters):
        self.calls.append(("search", (space_id,)))
        return []


def test_legacy_adapter_translates_scope_and_warns() -> None:
    """TC-S4-14：旧签名适配器可用 + 废弃告警 + 非原子声明。"""
    legacy = _LegacyStore()
    with pytest.warns(DeprecationWarning):
        adapter = LegacyEntityStoreAdapter(legacy, component="third-party")
    assert StoreCapability.ATOMIC_UNLINK not in adapter.capabilities()
    records = adapter.find_by_entity_text_hash(SCOPE, (HASH,), filters=FILTERS)
    assert [item.id for item in records] == ["e1"]
    assert legacy.calls[0] == ("find", ("acme", (HASH,)))
    adapter.execute_operations(
        SCOPE,
        [
            EntityOperation(
                type=EntityOpType.UNLINK, record_id="e1", unlink_memory_ids=("u1",)
            )
        ],
    )
    assert legacy.calls[-1][0] == "ops"
    assert legacy.calls[-1][1][0] == "acme"


def test_legacy_adapter_rejects_shards() -> None:
    with pytest.warns(DeprecationWarning):
        adapter = LegacyEntityStoreAdapter(_LegacyStore())
    with pytest.raises(UnsupportedStorageCapabilityError):
        adapter.find_by_entity_text_hash(
            SCOPE, (HASH,), filters=FILTERS, shards=frozenset({"working"})
        )


def test_shard_routed_rejects_legacy_inner() -> None:
    """TC-S4-10：包装层拒绝未声明 atomic_unlink 的内层。"""
    with pytest.warns(DeprecationWarning):
        adapter = LegacyEntityStoreAdapter(_LegacyStore())
    with pytest.raises(UnsupportedStorageCapabilityError):
        from jiuwen_memory.storage.entity_impl.shard_routed_entity import (
            ShardRoutedEntityStore,
        )

        ShardRoutedEntityStore(
            resolver=RuleBasedShardResolver(
                shards=["main"], default_shard="main", rules=[]
            ),
            locator=_NoopLocator(),
            mode="shared",
            inner=adapter,
        )


class _NoopLocator:
    @staticmethod
    def locate(scope, unit_ids):
        return ShardLocation()


def test_entity_unknown_shard_rejected() -> None:
    store = InMemoryEntityStore()
    with pytest.raises(ValidationError):
        from jiuwen_memory.storage.entity_impl.shard_routed_entity import (
            ShardRoutedEntityStore,
        )

        ShardRoutedEntityStore(
            resolver=RuleBasedShardResolver(
                shards=["main"], default_shard="main", rules=[]
            ),
            locator=_NoopLocator(),
            mode="split",
            stores={"main": store},
        ).find_by_entity_text_hash(
            SCOPE, (HASH,), filters=FILTERS, shards=frozenset({"ghost"})
        )


# ---------------------------------------------------------------------------
# ES 实体：分片视图 / 空记录 / 幂等 404（不连真实 ES，测纯函数）
# ---------------------------------------------------------------------------


def _es_hit(*, by_shard: dict[str, list[str]], union: list[str]) -> dict:
    return {
        "_id": "e1",
        "_source": {
            "space_id": "acme",
            "entity_text_hash": HASH,
            "entity_type": "PROPER",
            "linked_memory_ids": union,
            "linked_by_shard": by_shard,
            "actor_id": "u1",
        },
    }


def test_es_hit_projects_only_selected_shards() -> None:
    """P1：指定 shards 时 ES 返回的关联只能是选中分组的并集。"""
    from jiuwen_memory.storage.entity_impl.elasticsearch_entity_store import (
        ElasticsearchEntityStore,
    )

    hit = _es_hit(by_shard={"profile": ["u1"], "working": ["u2"]}, union=["u1", "u2"])
    assert getattr(ElasticsearchEntityStore, "_hit_to_entity_record")(
        hit, frozenset({"working"})
    ).linked_memory_ids == ("u2",)
    assert getattr(ElasticsearchEntityStore, "_hit_to_entity_record")(
        hit, frozenset({"main"})
    ).linked_memory_ids == ()


def test_es_bulk_404_tolerated_for_idempotent_ops() -> None:
    """P1：split 扇出的 UNLINK / DELETE_IF_EMPTY 在缺失文档上不得计为失败。"""
    from jiuwen_memory.storage.entity_impl.elasticsearch_entity_store import (
        ElasticsearchEntityStore,
    )

    response = {
        "items": [
            {"update": {"_id": "e1@working", "status": 404}},
            {"update": {"_id": "e1@profile", "status": 200}},
            {"create": {"_id": "e2@main", "status": 404}},
        ]
    }
    result = getattr(ElasticsearchEntityStore, "_parse_bulk_response_with_tolerance")(
        response, [("e1@working", True), ("e1@profile", True), ("e2@main", False)]
    )
    assert result.successful_ids == ["e1@working", "e1@profile"]
    assert result.failed_ids == ["e2@main"]


def test_legacy_adapter_is_configurable_from_yaml() -> None:
    """P1：legacy target 可从配置装配，并自动识别旧签名实现。"""
    from jiuwen_memory.config.context import AssemblyContext
    from jiuwen_memory.storage.entity_store import EntityStoreProducer

    with EntityStoreProducer.registration_scope():
        EntityStoreProducer.register("old_impl")(lambda config: _LegacyStore())
        with pytest.warns(DeprecationWarning):
            adapter = EntityStoreProducer.build(
                "legacy",
                {
                    "legacy_store": {"target": "old_impl"},
                    "component": "third-party",
                },
                AssemblyContext(),
            )
        assert adapter.store_type() is not None
        assert adapter.find_by_entity_text_hash(
            SCOPE, (HASH,), filters=FILTERS
        )[0].id == "e1"


def test_legacy_target_passes_through_new_signature() -> None:
    """新签名实现经 legacy target 时原样返回，不重复包装。"""
    from jiuwen_memory.config.context import AssemblyContext
    from jiuwen_memory.storage.entity_store import EntityStoreProducer

    inner = InMemoryEntityStore()
    adapter = EntityStoreProducer.build(
        "legacy", {"legacy_store": inner}, AssemblyContext()
    )
    assert adapter is inner
