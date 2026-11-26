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
    UnitCommitState,
    UnitCommitStatus,
    align_unit_receipts,
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


def test_build_grouped_indexes_committed_properties() -> None:
    """批量写入（add 的 grouped 路径）必须把已提交属性加进反向索引。

    回归：build_grouped 曾对 COMMITTED 单元调用 mark_forward_only，把成功写入检索
    索引的属性排除在反向索引之外，导致 CONFLICT_CONSOLIDATE 永远成不了簇。
    """
    kv = InMemoryKVStore()
    builder = _hybrid(kv)
    index = SchemaPropertyIndex(kv)
    first = _property("property-1", "person:alice")
    second = _property("property-2", "person:alice")

    statuses = builder.build_grouped([first, second])

    assert [status.state for status in statuses] == [
        UnitCommitState.COMMITTED,
        UnitCommitState.COMMITTED,
    ]
    lookup = index.lookup(first.scope, "person:alice")
    assert lookup.indexed is True
    assert sorted(lookup.unit_ids) == ["property-1", "property-2"]


class _FailAfterInsertKV(InMemoryKVStore):
    """写入真源后抛错：模拟「正排已落盘但批量回执只能报 UNKNOWN」的情况。"""

    def insert(self, scope, key, value, ttl: float = 0.0) -> None:
        super().insert(scope, key, value, ttl)
        if key.endswith("/property-b"):
            raise BackendError("injected failure after truth write")


def test_build_grouped_keeps_receipts_when_reverse_index_upsert_fails() -> None:
    """反向索引维护失败不能吞掉已确认的正排回执，也不能让调用方误以为没写。"""
    kv = InMemoryKVStore()
    builder = _hybrid(kv)
    scope = Scope(org="org", user="user")
    # 真源里一条无法解码的旧记录：首次全量扫描会抛 BackendError。
    kv.insert(scope, memory_key("legacy"), b"not-a-memory-unit")
    unit = _property("property-1", "person:alice", scope=scope)

    statuses = builder.build_grouped([unit])

    assert [status.state for status in statuses] == [UnitCommitState.COMMITTED]
    assert kv.exists(scope, memory_key("property-1")), "正排必须已经写入"
    assert SchemaPropertyIndex(kv).lookup(scope, "person:alice").indexed is False, (
        "索引维护失败后按 scope 失效，查询回退真源"
    )


class _WatermarkBrokenKV(InMemoryKVStore):
    """索引完整性标记的写入/删除失败：真源写入正常，索引维护与失效都失败。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail_index = False

    def _is_watermark(self, key: str) -> bool:
        return self.fail_index and key.endswith("/scope-watermark")

    def insert(self, scope, key, value, ttl: float = 0.0) -> None:
        if self._is_watermark(key):
            raise BackendError("injected watermark insert failure")
        super().insert(scope, key, value, ttl)

    def update(self, scope, key, value, ttl: float = 0.0) -> None:
        if self._is_watermark(key):
            raise BackendError("injected watermark update failure")
        super().update(scope, key, value, ttl)

    def delete(self, scope, key) -> None:
        if self._is_watermark(key):
            raise BackendError("injected watermark delete failure")
        super().delete(scope, key)


def test_build_grouped_attaches_index_error_when_invalidation_also_fails() -> None:
    """索引维护与失效都失败时，回执必须带错：不能把部分成功报告成完整成功。"""
    kv = _WatermarkBrokenKV()
    builder = _hybrid(kv)
    scope = Scope(org="org", user="user")
    first = _property("property-a", "person:alice", scope=scope)
    builder.build_grouped([first])

    kv.fail_index = True
    second = _property("property-b", "person:alice", scope=scope)
    statuses = builder.build_grouped([second])

    assert statuses[0].state is UnitCommitState.COMMITTED, "正排已确认，状态不能降级"
    assert statuses[0].error is not None, "索引维护/失效失败必须附到回执上"
    assert kv.exists(scope, memory_key("property-b")), "正排确实已写入"
    # 双故障下 watermark 删不掉，但修复标记已写入：读侧仍回退真源。
    assert SchemaPropertyIndex(kv).lookup(scope, "person:alice").indexed is False


@pytest.mark.parametrize(
    "receipts",
    [
        [
            UnitCommitStatus(unit_id="x", state=UnitCommitState.COMMITTED),
            UnitCommitStatus(unit_id="x", state=UnitCommitState.FAILED),
        ],
        [
            UnitCommitStatus(unit_id="x", state=UnitCommitState.FAILED),
            UnitCommitStatus(unit_id="x", state=UnitCommitState.COMMITTED),
        ],
    ],
)
def test_align_unit_receipts_rejects_contradictory_duplicates(receipts, caplog) -> None:
    """同一输入返回两份矛盾回执时，不能由回执顺序决定结果。"""
    unit = _property("x", "person:alice")

    with caplog.at_level("WARNING"):
        aligned = align_unit_receipts([unit], receipts)

    assert [status.state for status in aligned] == [UnitCommitState.UNKNOWN]
    assert any("receipt mismatch" in record.message for record in caplog.records), (
        "矛盾回执必须产生告警"
    )


def test_build_grouped_uses_aligned_positions_for_side_effects() -> None:
    """同名 id 的 [FAILED, COMMITTED] 不能被折叠成「两条都提交」。"""

    class _OrderedStatusBuilder(IndexBuilder):
        def __init__(self, kv: InMemoryKVStore) -> None:
            self._kv = kv

        @staticmethod
        def operator_type() -> OperatorType:
            return OperatorType.INDEX_BUILDER

        @staticmethod
        def health() -> None:
            return None

        @staticmethod
        def supports_grouped_build() -> bool:
            return True

        def build_grouped(self, units):
            # 只有第二条真源落盘；第一条 FAILED。
            second = units[1]
            self._kv.insert(second.scope, memory_key(second.id), dumps(second))
            return [
                UnitCommitStatus(unit_id=units[0].id, state=UnitCommitState.FAILED),
                UnitCommitStatus(unit_id=units[1].id, state=UnitCommitState.COMMITTED),
            ]

        @staticmethod
        def build(units, *, mode=None) -> None:
            return None

        @staticmethod
        def update(units, *, mode=None) -> None:
            return None

        @staticmethod
        def remove(units, *, mode=None) -> None:
            return None

        @staticmethod
        def rebuild() -> None:
            return None

    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    builder = SchemaPropertyIndexingBuilder(_OrderedStatusBuilder(kv), index)
    alice = Scope(org="org", user="alice")
    bob = Scope(org="org", user="bob")
    first = _property("x", "person:alice", scope=alice)
    second = _property("x", "person:bob", scope=bob)

    statuses = builder.build_grouped([first, second])

    assert [status.state for status in statuses] == [
        UnitCommitState.FAILED,
        UnitCommitState.COMMITTED,
    ]
    assert index.lookup(bob, "person:bob").unit_ids == ["x"]
    assert index.lookup(alice, "person:alice").indexed is False, (
        "FAILED 的同名项不能因为另一 scope 的 COMMITTED 而进入索引"
    )


def test_align_unit_receipts_does_not_guess_ambiguous_ids() -> None:
    """数量不符且 id 无法唯一匹配时，两项都必须是 UNKNOWN。"""
    alice = Scope(org="org", user="alice")
    bob = Scope(org="org", user="bob")
    units = [
        _property("x", "person:alice", scope=alice),
        _property("x", "person:bob", scope=bob),
    ]

    missing = align_unit_receipts(
        units, [UnitCommitStatus(unit_id="x", state=UnitCommitState.COMMITTED)]
    )

    assert [status.state for status in missing] == [
        UnitCommitState.UNKNOWN,
        UnitCommitState.UNKNOWN,
    ]


def test_build_grouped_keeps_same_unit_id_across_scopes() -> None:
    """unit id 只在 scope 内唯一：跨 scope 同名 id 不能被当成同一份回执。"""
    kv = InMemoryKVStore()
    builder = _hybrid(kv)
    alice = Scope(org="org", user="alice")
    bob = Scope(org="org", user="bob")
    first = _property("same", "person:alice", scope=alice)
    second = _property("same", "person:bob", scope=bob)

    statuses = builder.build_grouped([first, second])

    assert [status.state for status in statuses] == [
        UnitCommitState.COMMITTED,
        UnitCommitState.COMMITTED,
    ]
    index = SchemaPropertyIndex(kv)
    assert index.lookup(alice, "person:alice").unit_ids == ["same"]
    assert index.lookup(bob, "person:bob").unit_ids == ["same"]


def test_build_grouped_aligns_receipts_by_unit_id() -> None:
    """第三方 builder 漏回执时，不能把别的 unit 的 COMMITTED 套到它身上。"""

    class _ShortReceiptBuilder(IndexBuilder):
        def __init__(self, kv: InMemoryKVStore) -> None:
            self._kv = kv

        @staticmethod
        def operator_type() -> OperatorType:
            return OperatorType.INDEX_BUILDER

        @staticmethod
        def health() -> None:
            return None

        @staticmethod
        def supports_grouped_build() -> bool:
            return True

        def build_grouped(self, units):
            for unit in units:
                self._kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
            # 漏回第一条：模拟仓库外 builder 少回一项。
            return [
                UnitCommitStatus(unit_id=unit.id, state=UnitCommitState.COMMITTED)
                for unit in units[1:]
            ]

        @staticmethod
        def build(units, *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
            return None

        @staticmethod
        def update(units, *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
            return None

        @staticmethod
        def remove(units, *, mode: IndexRemoveMode = IndexRemoveMode.HARD) -> None:
            return None

        @staticmethod
        def rebuild() -> None:
            return None

    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    scope = Scope(org="org", user="user")
    seeded = _property("property-a", "person:alice", scope=scope)
    _hybrid(kv).build_grouped([seeded])
    assert index.lookup(scope, "person:alice").unit_ids == ["property-a"]

    builder = SchemaPropertyIndexingBuilder(_ShortReceiptBuilder(kv), index)
    missing = _property("property-b", "person:alice", scope=scope)
    present = _property("property-c", "person:alice", scope=scope)
    statuses = builder.build_grouped([missing, present])

    by_id = {status.unit_id: status for status in statuses}
    assert by_id["property-b"].state is UnitCommitState.UNKNOWN, "漏回执按 UNKNOWN 处理"
    assert by_id["property-b"].error is not None
    assert by_id["property-c"].state is UnitCommitState.COMMITTED
    # 含 UNKNOWN 的 scope 必须回退真源：旧实现会把 C 的 COMMITTED 套给 B 并继续信任索引。
    assert index.lookup(scope, "person:alice").indexed is False


def test_build_grouped_invalidates_scope_when_any_unit_unknown() -> None:
    """同批含 UNKNOWN 时，旧索引可能缺成员，必须失效让查询回退真源。"""
    kv = _FailAfterInsertKV()
    builder = _hybrid(kv)
    index = SchemaPropertyIndex(kv)
    scope = Scope(org="org", user="user")
    first = _property("property-a", "person:alice", scope=scope)
    builder.build_grouped([first])
    assert index.lookup(scope, "person:alice").unit_ids == ["property-a"]

    second = _property("property-b", "person:alice", scope=scope)
    statuses = builder.build_grouped([second])

    assert statuses[0].state is UnitCommitState.UNKNOWN
    assert kv.exists(scope, memory_key("property-b")), "UNKNOWN 的正排可能已经落盘"
    assert index.lookup(scope, "person:alice").indexed is False, (
        "含 UNKNOWN 的 scope 必须失效，不能让查询信任缺成员的旧索引"
    )


def test_repair_scope_clears_exclusions_and_rebuilds() -> None:
    """旧版本坏数据的一次性修复入口：清 exclusion，失效后立即按真源重建。"""
    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    unit = _property("property-1", "person:alice")
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    index.mark_forward_only([unit])
    assert index.lookup(unit.scope, "person:alice").unit_ids == []
    assert kv.scan(unit.scope, "/schema/entity-properties/v1/excluded/")

    index.repair_scope(unit.scope)

    assert kv.scan(unit.scope, "/schema/entity-properties/v1/excluded/") == []
    lookup = index.lookup(unit.scope, "person:alice")
    assert lookup.indexed is True, "repair 后应立即重建，不必等下一次写入"
    assert lookup.unit_ids == ["property-1"]
    assert not kv.exists(unit.scope, "/schema/entity-properties/v1/needs-repair")


def test_repair_scope_keeps_marker_when_rebuild_fails() -> None:
    """重建失败时 needs-repair 必须保留，读侧继续回退真源。"""
    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    unit = _property("property-1", "person:alice")
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    index.mark_forward_only([unit])
    kv.insert(unit.scope, memory_key("corrupted"), b"not-a-memory-unit")

    with pytest.raises(BackendError):
        index.repair_scope(unit.scope)

    assert kv.exists(unit.scope, "/schema/entity-properties/v1/needs-repair")
    assert index.lookup(unit.scope, "person:alice").indexed is False


class _OpRecordingKV(InMemoryKVStore):
    def __init__(self) -> None:
        super().__init__()
        self.ops: list[tuple[str, str]] = []

    def insert(self, scope, key, value, ttl: float = 0.0) -> None:
        self.ops.append(("write", key))
        super().insert(scope, key, value, ttl)

    def update(self, scope, key, value, ttl: float = 0.0) -> None:
        self.ops.append(("write", key))
        super().update(scope, key, value, ttl)

    def delete(self, scope, key) -> None:
        self.ops.append(("delete", key))
        super().delete(scope, key)


def test_repair_scope_restores_archived_members() -> None:
    """被误排除、之后又归档的成员也要恢复：归档成员留在索引里供历史回看。"""
    kv = InMemoryKVStore()
    index = SchemaPropertyIndex(kv)
    scope = Scope(org="org", user="user")
    archived = _property("archived", "person:alice", scope=scope)
    archived.lifecycle = LifecycleState.ARCHIVED
    active = _property("active", "person:alice", scope=scope)
    for unit in (archived, active):
        kv.insert(scope, memory_key(unit.id), dumps(unit))
    index.mark_forward_only([archived, active])

    index.repair_scope(scope)

    lookup = index.lookup(scope, "person:alice")
    assert lookup.indexed is True
    assert lookup.unit_ids == ["active", "archived"], (
        "正常归档不写 exclusion；修复必须恢复全部误排除成员，含已归档的"
    )


def test_bootstrap_keeps_repair_marker_until_watermark_committed() -> None:
    """needs-repair 必须活到新 watermark 落盘之后，扫除与 commit 都不能提前删。"""
    kv = _OpRecordingKV()
    index = SchemaPropertyIndex(kv)
    scope = Scope(org="org", user="user")
    unit = _property("property-1", "person:alice", scope=scope)
    kv.insert(scope, memory_key(unit.id), dumps(unit))
    index.invalidate_scope(scope)
    kv.ops.clear()

    index.upsert([unit])

    repair = "/schema/entity-properties/v1/needs-repair"
    watermark = "/schema/entity-properties/v1/scope-watermark"
    repair_deletes = [
        position
        for position, (op, key) in enumerate(kv.ops)
        if op == "delete" and key == repair
    ]
    watermark_writes = [
        position
        for position, (op, key) in enumerate(kv.ops)
        if op == "write" and key == watermark
    ]
    assert len(repair_deletes) == 1, "标记只能由 commit 清一次"
    assert watermark_writes and repair_deletes[0] > watermark_writes[-1], (
        "新 watermark 写成功后才能清 needs-repair"
    )
    assert index.lookup(scope, "person:alice").indexed is True


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
        StoreManagerProducer.put("default", CompositeStoreManager(), ctx=context)

        builder = IndexBuilderProducer.build("schema-property-test-noop", {}, context)

        assert isinstance(builder, _NoopBuilder)
    finally:
        Factory.reset_all()
