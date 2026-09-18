"""Schema Entity → Property MemoryUnit KV 反向索引测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.chunker.chunker_impl.recursive_chunker import RecursiveChunker
from jiuwen_memory.common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import (
    MEMORY_KEY_PREFIX,
    LifecycleState,
    MemoryUnit,
    Scope,
    Segment,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.bootstrap import register_constructors
from jiuwen_memory.construction.index_builder import (
    IndexBuilder,
    IndexBuilderProducer,
    SchemaPropertyIndexingBuilder,
)
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.store_manager import StoreManagerProducer
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

pytestmark = pytest.mark.unit


def _property(
    unit_id: str,
    entity_key: str,
    *,
    scope: Scope | None = None,
) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=scope or Scope(org="org", user="user"),
        segments=[Segment(content="Alice likes hiking")],
        system_metadata={
            "extraction_mode": "schema",
            "schema_entity_key": entity_key,
            "schema_entity_name": "Alice",
            "schema_entity_type": "person",
            "schema_property_name": "hobby",
        },
    )


def _plain_hybrid(kv: InMemoryKVStore) -> HybridIndexBuilder:
    tokenizer = WhitespaceTokenizer()
    manager = CompositeStoreManager(
        kv=kv,
        vector=InMemoryVectorStore(),
        fulltext=InMemoryFulltextStore(tokenizer),
    )
    return HybridIndexBuilder(
        manager,
        chunker=RecursiveChunker(
            chunk_size_chars=50,
            overlap_chars=10,
            min_chunk_chars=5,
        ),
        embedder=HashingEmbedder(tokenizer),
    )


def _hybrid(kv: InMemoryKVStore) -> IndexBuilder:
    return SchemaPropertyIndexingBuilder(_plain_hybrid(kv), SchemaPropertyIndex(kv))


def test_lookup_distinguishes_missing_index_from_authoritative_empty() -> None:
    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    unit = _property("property-1", "person:alice")

    assert index.lookup(unit.scope, "person:alice").indexed is False

    index.upsert([unit])
    populated = index.lookup(unit.scope, "person:alice")
    assert populated.indexed is True
    assert populated.unit_ids == ["property-1"]

    index.remove([unit])
    empty = index.lookup(unit.scope, "person:alice")
    assert empty.indexed is True
    assert empty.unit_ids == []


def test_index_is_scope_isolated_and_returns_stable_unique_ids() -> None:
    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    scope_a = Scope(org="org", space="a", user="user")
    scope_b = Scope(org="org", space="b", user="user")

    index.upsert(
        [
            _property("property-2", "person:alice", scope=scope_a),
            _property("property-1", "person:alice", scope=scope_a),
            _property("property-1", "person:alice", scope=scope_a),
            _property("property-3", "person:alice", scope=scope_b),
        ]
    )

    assert index.lookup(scope_a, "person:alice").unit_ids == ["property-1", "property-2"]
    assert index.lookup(scope_b, "person:alice").unit_ids == ["property-3"]


def test_upsert_moves_membership_when_entity_changes() -> None:
    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    old = _property("property-1", "person:alice")
    new = _property("property-1", "person:alice-smith")

    index.upsert([old])
    index.upsert([new])

    old_lookup = index.lookup(old.scope, "person:alice")
    assert old_lookup.indexed is True
    assert old_lookup.unit_ids == []
    assert index.lookup(new.scope, "person:alice-smith").unit_ids == ["property-1"]


def test_upsert_non_schema_unit_removes_previous_membership() -> None:
    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    old = _property("property-1", "person:alice")
    ordinary = MemoryUnit(
        id=old.id,
        scope=old.scope,
        segments=[Segment(content="ordinary")],
    )

    index.upsert([old])
    index.upsert([ordinary])

    lookup = index.lookup(old.scope, "person:alice")
    assert lookup.indexed is True
    assert lookup.unit_ids == []


def test_hybrid_maintains_index_only_on_retrieval_write_paths() -> None:
    kv = InMemoryKVStore()
    builder = _hybrid(kv)
    index = SchemaPropertyIndex(kv)
    forward_only = _property("forward-only", "person:alice")
    retrieval_only = _property("retrieval-only", "person:bob")

    builder.build([forward_only], mode=IndexWriteMode.FORWARD_ONLY)
    builder.build([retrieval_only], mode=IndexWriteMode.RETRIEVAL_ONLY)

    forward_lookup = index.lookup(forward_only.scope, "person:alice")
    assert forward_lookup.indexed is True
    assert forward_lookup.unit_ids == []
    assert index.lookup(retrieval_only.scope, "person:bob").unit_ids == ["retrieval-only"]


def test_hybrid_update_moves_membership_and_remove_respects_mode() -> None:
    kv = InMemoryKVStore()
    builder = _hybrid(kv)
    index = SchemaPropertyIndex(kv)
    original = _property("property-1", "person:alice")
    moved = _property("property-1", "person:alice-smith")

    builder.build([original])
    builder.update([moved])
    assert index.lookup(original.scope, "person:alice").unit_ids == []
    assert index.lookup(moved.scope, "person:alice-smith").unit_ids == ["property-1"]

    builder.remove([moved], mode=IndexRemoveMode.SOFT)
    assert index.lookup(moved.scope, "person:alice-smith").unit_ids == ["property-1"]

    builder.remove([moved], mode=IndexRemoveMode.HARD)
    lookup = index.lookup(moved.scope, "person:alice-smith")
    assert lookup.indexed is True
    assert lookup.unit_ids == []


def test_forward_only_lifecycle_update_retains_history_membership() -> None:
    kv = InMemoryKVStore()
    builder = _hybrid(kv)
    index = SchemaPropertyIndex(kv)
    unit = _property("property-1", "person:alice")
    builder.build([unit])
    unit.lifecycle = LifecycleState.ARCHIVED

    builder.update([unit], mode=IndexWriteMode.FORWARD_ONLY)
    builder.remove([unit], mode=IndexRemoveMode.SOFT)

    assert index.lookup(unit.scope, "person:alice").unit_ids == ["property-1"]


def test_plain_builder_does_not_touch_schema_kv_when_feature_is_disabled() -> None:
    kv = InMemoryKVStore()
    builder = _plain_hybrid(kv)

    builder.build([_property("property-1", "person:alice")])

    assert kv.scan(Scope(org="org", user="user"), "/schema/") == []


def test_remove_with_scope_cleans_reverse_membership() -> None:
    kv = InMemoryKVStore()
    builder = _hybrid(kv)
    unit = _property("property-1", "person:alice")

    builder.build([unit])
    builder.remove_with_scope([unit.id], unit.scope)

    lookup = SchemaPropertyIndex(kv).lookup(unit.scope, "person:alice")
    assert lookup.indexed is True
    assert lookup.unit_ids == []


def test_identity_prefers_entity_id_and_requires_property_name() -> None:
    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    unit = _property("property-1", "temporary-key")
    unit.system_metadata["schema_entity_id"] = "canonical-id"
    invalid = _property("not-a-property", "temporary-key")
    invalid.system_metadata["schema_property_name"] = ""

    index.upsert([unit, invalid])

    assert index.lookup(unit.scope, "canonical-id").unit_ids == ["property-1"]
    assert index.lookup(unit.scope, "temporary-key").unit_ids == []


def test_identity_uses_same_legacy_fallback_as_reader() -> None:
    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    unit = _property("property-1", "unused")
    unit.system_metadata.pop("schema_entity_key")
    unit.system_metadata["schema_entity_name"] = "  Alice\u3000Smith  "

    index.upsert([unit])

    assert index.lookup(unit.scope, "person::Alice Smith").unit_ids == ["property-1"]


def test_first_scope_watermark_backfills_legacy_properties_before_becoming_authoritative() -> None:
    kv = InMemoryKVStore()
    builder = _hybrid(kv)
    index = SchemaPropertyIndex(kv)
    legacy = _property("legacy-property", "person:alice")
    current = _property("current-property", "person:alice")
    kv.insert(legacy.scope, memory_key(legacy.id), dumps(legacy))

    builder.build([current])

    lookup = index.lookup(current.scope, "person:alice")
    assert lookup.indexed is True
    assert lookup.unit_ids == ["current-property", "legacy-property"]


class _CountingKV(InMemoryKVStore):
    def __init__(self) -> None:
        super().__init__()
        self.memory_scans = 0

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        if prefix == MEMORY_KEY_PREFIX:
            self.memory_scans += 1
        return super().scan(scope, prefix)


class _FailingWatermarkKV(InMemoryKVStore):
    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        if key.endswith("/scope-watermark"):
            raise BackendError("injected watermark failure")
        super().insert(scope, key, value, ttl)


class _PartiallyFailingBuilder(IndexBuilder):
    def __init__(self, kv: InMemoryKVStore) -> None:
        self._kv = kv

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(
        self,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
    ) -> None:
        unit = units[0]
        self._kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
        raise BackendError("injected delegate failure")

    def update(
        self,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
    ) -> None:
        self.build(units, mode=mode)

    def remove(
        self,
        units: list[MemoryUnit],
        *,
        mode: IndexRemoveMode = IndexRemoveMode.HARD,
    ) -> None:
        raise BackendError("injected delegate failure")

    def rebuild(self) -> None:
        return None


class _NoopBuilder(IndexBuilder):
    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(
        self,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
    ) -> None:
        return None

    def update(
        self,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
    ) -> None:
        return None

    def remove(
        self,
        units: list[MemoryUnit],
        *,
        mode: IndexRemoveMode = IndexRemoveMode.HARD,
    ) -> None:
        return None

    def rebuild(self) -> None:
        return None


@IndexBuilderProducer.register("schema-property-test-noop")
def _build_noop(_config) -> IndexBuilder:
    return _NoopBuilder()


def test_scope_watermark_bootstraps_all_entities_with_one_truth_scan() -> None:
    kv = _CountingKV()
    index = SchemaPropertyIndex(kv)
    alice = _property("alice-property", "person:alice")
    bob = _property("bob-property", "person:bob")
    kv.insert(alice.scope, memory_key(alice.id), dumps(alice))
    kv.insert(bob.scope, memory_key(bob.id), dumps(bob))

    index.upsert([alice])
    index.upsert([bob])

    assert kv.memory_scans == 1
    assert index.lookup(alice.scope, "person:alice").unit_ids == ["alice-property"]
    assert index.lookup(bob.scope, "person:bob").unit_ids == ["bob-property"]


def test_partial_bootstrap_never_publishes_authoritative_watermark() -> None:
    kv = _FailingWatermarkKV()
    index = SchemaPropertyIndex(kv)
    unit = _property("property-1", "person:alice")

    with pytest.raises(BackendError, match="watermark failure"):
        index.upsert([unit])

    assert index.lookup(unit.scope, "person:alice").indexed is False


def test_partial_delegate_write_invalidates_existing_scope_watermark() -> None:
    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    existing = _property("existing", "person:alice")
    index.upsert([existing])
    builder = SchemaPropertyIndexingBuilder(_PartiallyFailingBuilder(kv), index)

    with pytest.raises(BackendError, match="delegate failure"):
        builder.build([_property("partial", "person:bob")])

    assert index.lookup(existing.scope, "person:alice").indexed is False


def test_concurrent_targets_share_scope_bootstrap_and_keep_all_memberships() -> None:
    kv = _CountingKV()
    first = _property("property-1", "person:alice")
    second = _property("property-2", "person:bob")
    for unit in (first, second):
        kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    indexes = [SchemaPropertyIndex(kv), SchemaPropertyIndex(kv)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(index.upsert, [unit])
            for index, unit in zip(indexes, (first, second), strict=True)
        ]
        for future in futures:
            future.result()

    reader = SchemaPropertyIndex(kv)
    assert kv.memory_scans == 1
    assert reader.lookup(first.scope, "person:alice").unit_ids == ["property-1"]
    assert reader.lookup(second.scope, "person:bob").unit_ids == ["property-2"]


def test_producer_gate_wraps_mixed_targets_only_when_temporal_is_enabled() -> None:
    register_plugins()
    register_backends()
    register_constructors()
    Factory.reset_all()
    try:
        disabled_context = default_context()
        disabled = IndexBuilderProducer.build("forward", {}, disabled_context)
        assert not isinstance(disabled, SchemaPropertyIndexingBuilder)

        Factory.reset_all()
        enabled_context = default_context()
        enabled_context.globals["schema_enabled"] = True
        forward = IndexBuilderProducer.build("forward", {}, enabled_context)
        hybrid = IndexBuilderProducer.build(
            "hybrid",
            {"chunker": "default", "embedder": "default"},
            enabled_context,
        )
        assert isinstance(forward, SchemaPropertyIndexingBuilder)
        assert isinstance(hybrid, SchemaPropertyIndexingBuilder)

        alice = _property("alice-property", "person:alice")
        bob = _property("bob-property", "person:bob")
        forward.build([alice])
        hybrid.build([bob])

        manager = StoreManagerProducer.build_named("default", enabled_context)
        index = SchemaPropertyIndex(manager.kv())
        assert index.lookup(alice.scope, "person:alice").unit_ids == ["alice-property"]
        assert index.lookup(bob.scope, "person:bob").unit_ids == ["bob-property"]
    finally:
        Factory.reset_all()


def test_producer_gate_allows_enabled_manager_without_kv() -> None:
    register_plugins()
    register_backends()
    register_constructors()
    Factory.reset_all()
    try:
        context = default_context()
        context.globals["schema_enabled"] = True
        StoreManagerProducer.put("default", CompositeStoreManager())

        builder = IndexBuilderProducer.build("schema-property-test-noop", {}, context)

        assert isinstance(builder, _NoopBuilder)
    finally:
        Factory.reset_all()
