"""EntityLinkService 单元测试。

实体反向索引写入侧编排。覆盖 hash 精确全路径：hash 命中 → LINK；未命中 →
INSERT；以及 unlink_memory 的 UNLINK_UPDATE / DELETE 分支。全走公开 API
（``link_memories`` / ``unlink_memory``），不访问受保护成员。

ES 后端用内存版假 EntityStore 替代（测试环境无 ES），实现 EntityStore 端口
四方法 + BaseStore 契约。归并只走 hash 精确匹配（2026-08-12 改造砍掉
向量归并后，hash 未命中直接当新实体 INSERT）。``EntityLinkService`` 只消费
``unit.entities`` 明文（砍掉 spaCy 兜底），构造函数已删除 ``extractor`` 参数。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Segment,
)
from jiuwen_memory.common.type_def.entity import (
    EntityBatchResult,
    EntityMention,
    EntityOpType,
    EntityOperation,
    EntityRecord,
    EntityStoreFilters,
    hash_entity_text,
)
from jiuwen_memory.common.type_def.normalizer import EntityNormalizer
from jiuwen_memory.common.type_def.scope import Scope, space_id_from_scope
from jiuwen_memory.construction.index_builder_impl.entity_index_builder import (
    EntityIndexAdmissionPolicy,
    EntityLinkService,
)
from jiuwen_memory.storage.base import BaseStore, StoreType

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 内存版假 EntityStore（实现 EntityStore 端口，供 linker/recaller 测试复用）
# ---------------------------------------------------------------------------


class InMemoryEntityStore(BaseStore):
    """entity 索引的内存实现，语义对齐 ElasticsearchEntityStore 的可观测行为。

    维护 ``_records: dict[id, EntityRecord]``，按 space_id 隔离命名空间。
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, EntityRecord]] = {}
        self._ready = True

    # -- BaseStore 契约 --
    @staticmethod
    def store_type() -> StoreType | None:
        # ES 实现返回 None（entity 不在 StoreType 枚举，独立端口）
        return None

    @staticmethod
    def health() -> None:
        return None

    # -- EntityStore 端口 --
    def ensure_index(self) -> None:
        self._ready = True

    def _space(self, space_id: str) -> dict[str, EntityRecord]:
        return self._records.setdefault(space_id, {})

    def records(self, space_id: str) -> list[EntityRecord]:
        """公开只读视图：返回某 space 下全部实体记录（测试断言用）。"""
        return list(self._space(space_id).values())

    def find_by_entity_text_hash(
        self,
        space_id: str,
        entity_text_hashes: tuple[str, ...],
        *,
        filters: EntityStoreFilters,
        limit: int = 500,
    ) -> list[EntityRecord]:
        want = set(entity_text_hashes)
        out: list[EntityRecord] = []
        for rec in self._space(space_id).values():
            if rec.entity_text_hash in want and self._matches_filters(rec, filters):
                out.append(rec)
                if len(out) >= limit:
                    break
        return out

    def find_by_linked_memory_id(self, space_id: str, memory_id: str) -> list[EntityRecord]:
        return [
            rec for rec in self._space(space_id).values()
            if memory_id in rec.linked_memory_ids
        ]

    def execute_operations(
        self,
        space_id: str,
        operations: list[EntityOperation],
    ) -> EntityBatchResult:
        space = self._space(space_id)
        successful: list[str] = []
        failed: list[str] = []
        for op in operations:
            try:
                if op.type is EntityOpType.INSERT:
                    assert op.record is not None
                    space[op.record.id] = op.record
                    successful.append(op.record.id)
                elif op.type is EntityOpType.LINK:
                    assert op.record_id is not None
                    rec = space[op.record_id]
                    merged = tuple(sorted(set(rec.linked_memory_ids) | set(op.link_memory_ids)))
                    space[op.record_id] = replace(rec, linked_memory_ids=merged)
                    successful.append(op.record_id)
                elif op.type is EntityOpType.UNLINK_UPDATE:
                    assert op.record is not None
                    space[op.record.id] = op.record
                    successful.append(op.record.id)
                elif op.type is EntityOpType.DELETE:
                    assert op.record_id is not None
                    space.pop(op.record_id, None)
                    successful.append(op.record_id)
            except Exception:
                oid = op.record_id if op.record_id is not None else (op.record.id if op.record else "?")
                failed.append(str(oid))
        return EntityBatchResult(successful_ids=successful, failed_ids=failed)

    @staticmethod
    def _matches_filters(rec: EntityRecord, filters: EntityStoreFilters) -> bool:
        # actor 单段 term 过滤；None 表示该维度不过滤
        if filters.actor_id is not None and rec.filters.actor_id != filters.actor_id:
            return False
        return True


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------


def _make_unit(
    uid: str,
    content: str,
    *,
    tier: MemoryTier = MemoryTier.SEMANTIC,
    entities: list[str] | None = None,
    scope: Scope = Scope(org="o1", user="u1", agent="a1", session="s1"),
) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=scope,
        tier=tier,
        segments=[Segment(content=content)],
        entities=list(entities or []),
    )


def _linker(
    store: InMemoryEntityStore | None = None,
) -> EntityLinkService:
    return EntityLinkService(
        entity_store=store or InMemoryEntityStore(),
        admission_policy=EntityIndexAdmissionPolicy(),
    )


@pytest.fixture
def store() -> InMemoryEntityStore:
    return InMemoryEntityStore()


# ---------------------------------------------------------------------------
# link_memories：INSERT 路径（新实体首次写入）
# ---------------------------------------------------------------------------


def test_link_inserts_new_entity(store: InMemoryEntityStore) -> None:
    linker = _linker(store)
    unit = _make_unit("u1", "Alice", entities=["Alice"])

    result = linker.link_memories([unit])

    assert result.extracted_count == 1
    assert result.inserted_count == 1
    assert result.failed_count == 0
    # 落库：一条 entity 记录，linked_memory_ids 含 u1
    records = store.records(space_id_from_scope(unit.scope))
    assert len(records) == 1
    assert "u1" in records[0].linked_memory_ids
    assert records[0].entity_text_hash == _hash("alice")


def test_link_dedupes_entities_within_one_unit(store: InMemoryEntityStore) -> None:
    # 同一 unit 多个 entity 归一化后相同 → 合并到一条记录
    linker = _linker(store)
    unit = _make_unit("u1", "x", entities=["Alice", "alice", "ALICE"])

    result = linker.link_memories([unit])

    # 三个写法归一化后都是 "alice"，去重为一条 entity
    assert result.extracted_count == 1
    assert result.inserted_count == 1
    records = store.records(space_id_from_scope(unit.scope))
    assert len(records) == 1


def test_link_multiple_distinct_entities(store: InMemoryEntityStore) -> None:
    linker = _linker(store)
    unit = _make_unit("u1", "x", entities=["Alice", "Paris", "Python"])

    result = linker.link_memories([unit])

    assert result.extracted_count == 3
    assert result.inserted_count == 3
    records = store.records(space_id_from_scope(unit.scope))
    assert len(records) == 3


# ---------------------------------------------------------------------------
# link_memories：LINK 路径（hash 精确命中已存实体）
# ---------------------------------------------------------------------------


def test_link_hashes_match_appends_memory_id(store: InMemoryEntityStore) -> None:
    # 先写 u1（含 Alice），再写 u2（同样含 Alice）→ hash 精确命中 → LINK
    linker = _linker(store)
    u1 = _make_unit("u1", "x", entities=["Alice"])
    u2 = _make_unit("u2", "x", entities=["Alice"])

    linker.link_memories([u1])
    result = linker.link_memories([u2])

    assert result.inserted_count == 0
    assert result.updated_count == 1  # LINK 计 updated
    records = store.records(space_id_from_scope(u1.scope))
    assert len(records) == 1  # 仍只有一条 entity 记录
    assert set(records[0].linked_memory_ids) == {"u1", "u2"}


def test_link_hash_match_skips_already_linked_id(store: InMemoryEntityStore) -> None:
    # 同一 unit.id 重复 link 同一实体 → 不产生 LINK op（ids_to_add 为空）
    linker = _linker(store)
    u1 = _make_unit("u1", "x", entities=["Alice"])

    first = linker.link_memories([u1])
    second = linker.link_memories([u1])

    assert first.inserted_count == 1
    # 第二次：hash 命中且 u1 已在 linked_memory_ids 里 → 无新增 op
    assert second.updated_count == 0
    assert second.inserted_count == 0


# ---------------------------------------------------------------------------
# link_memories：分组与准入
# ---------------------------------------------------------------------------


def test_link_groups_by_scope_filters(store: InMemoryEntityStore) -> None:
    # 不同 scope 的同实体分到不同组，filters 不串
    linker = _linker(store)
    u1 = _make_unit("u1", "x", entities=["Alice"],
                    scope=Scope(org="o1", user="u1", agent="a1", session="s1"))
    u2 = _make_unit("u2", "x", entities=["Alice"],
                    scope=Scope(org="o1", user="u2", agent="a1", session="s2"))

    result = linker.link_memories([u1, u2])

    # 两条都 INSERT（不同 session 隔离，各自建实体记录）
    assert result.inserted_count == 2
    assert result.updated_count == 0


def test_link_skips_non_admitted_tiers(store: InMemoryEntityStore) -> None:
    linker = _linker(store)
    # WORKING / ARCHIVAL 不准入
    u_work = _make_unit("uw", "x", entities=["Alice"], tier=MemoryTier.WORKING)
    u_arch = _make_unit("ua", "x", entities=["Bob"], tier=MemoryTier.ARCHIVAL)

    result = linker.link_memories([u_work, u_arch])

    assert result.extracted_count == 0
    assert result.inserted_count == 0
    assert result.skipped_count == 0  # EntityLinkResult 默认 0（skipped 不累计回传）
    assert store.records(space_id_from_scope(u_work.scope)) == []


def test_link_empty_units_returns_empty_result(store: InMemoryEntityStore) -> None:
    linker = _linker(store)
    result = linker.link_memories([])
    assert result.extracted_count == 0
    assert result.inserted_count == 0


def test_link_unit_without_entities_is_skipped(store: InMemoryEntityStore) -> None:
    # entities 为空 → 跳过该 unit（已砍 spaCy 兜底，无 extractor 回退）
    linker = _linker(store)
    unit = _make_unit("u1", "no entities here", entities=[])

    result = linker.link_memories([unit])

    assert result.extracted_count == 0
    assert result.inserted_count == 0
    assert store.records(space_id_from_scope(unit.scope)) == []


# ---------------------------------------------------------------------------
# unlink_memory：UNLINK_UPDATE / DELETE 分支
# ---------------------------------------------------------------------------


def test_unlink_update_when_other_memories_remain(store: InMemoryEntityStore) -> None:
    # 实体关联 u1, u2；删 u1 → 剩 u2 → UNLINK_UPDATE
    linker = _linker(store)
    u1 = _make_unit("u1", "x", entities=["Alice"])
    u2 = _make_unit("u2", "x", entities=["Alice"])
    linker.link_memories([u1, u2])

    result = linker.unlink_memory(
        space_id=space_id_from_scope(u1.scope), memory_id="u1",
    )

    assert result.updated_count == 1
    assert result.deleted_count == 0
    records = store.records(space_id_from_scope(u1.scope))
    assert len(records) == 1
    assert records[0].linked_memory_ids == ("u2",)


def test_unlink_delete_when_no_memory_remains(store: InMemoryEntityStore) -> None:
    # 实体只关联 u1；删 u1 → 空 → DELETE
    linker = _linker(store)
    u1 = _make_unit("u1", "x", entities=["Alice"])
    linker.link_memories([u1])

    result = linker.unlink_memory(
        space_id=space_id_from_scope(u1.scope), memory_id="u1",
    )

    assert result.deleted_count == 1
    assert result.updated_count == 0
    assert store.records(space_id_from_scope(u1.scope)) == []


def test_unlink_unknown_memory_id_is_noop(store: InMemoryEntityStore) -> None:
    linker = _linker(store)
    u1 = _make_unit("u1", "x", entities=["Alice"])
    linker.link_memories([u1])

    result = linker.unlink_memory(
        space_id=space_id_from_scope(u1.scope), memory_id="nonexistent",
    )

    assert result.updated_count == 0
    assert result.deleted_count == 0
    assert result.failed_count == 0
    # 原记录不变
    records = store.records(space_id_from_scope(u1.scope))
    assert len(records) == 1
    assert "u1" in records[0].linked_memory_ids


def test_unlink_removes_only_targeted_memory_id(store: InMemoryEntityStore) -> None:
    # 多实体各关联多个 unit，删一个 unit 只影响含它的实体
    linker = _linker(store)
    u1 = _make_unit("u1", "x", entities=["Alice", "Paris"])
    u2 = _make_unit("u2", "x", entities=["Alice", "Berlin"])
    linker.link_memories([u1, u2])

    result = linker.unlink_memory(
        space_id=space_id_from_scope(u1.scope), memory_id="u1",
    )

    # Alice 关联 u1,u2 → 删 u1 后剩 u2 → UNLINK_UPDATE
    # Paris 只关联 u1 → 删 u1 后空 → DELETE
    # Berlin 只关联 u2 → 不受影响
    assert result.updated_count == 1
    assert result.deleted_count == 1


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _hash(name: str) -> str:
    return hash_entity_text(EntityNormalizer.normalize(name))
