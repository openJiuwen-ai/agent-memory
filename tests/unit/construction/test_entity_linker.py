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
    EntityIndexBuilder,
    EntityLinkService,
)
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.entity_store import EntityStore

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 内存版假 EntityStore（实现 EntityStore 端口，供 linker/recaller 测试复用）
# ---------------------------------------------------------------------------


class InMemoryEntityStore(EntityStore):
    """entity 索引的内存实现，语义对齐 ElasticsearchEntityStore 的可观测行为。

    维护 ``_records: dict[id, EntityRecord]``，按 space_id 隔离命名空间。
    继承 ``EntityStore``（而非 ``BaseStore``）以跟随端口 ABC 的抽象方法集。
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, EntityRecord]] = {}
        self._ready = True

    # -- BaseStore 契约 --
    @staticmethod
    def store_type() -> StoreType:
        # F07-D 起 entity 是 StorageCapability/StoreType 第七席
        return StoreType.ENTITY

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

    def find_by_linked_memory_id(
        self,
        space_id: str,
        memory_id: str,
        *,
        filters: EntityStoreFilters,
    ) -> list[EntityRecord]:
        return [
            rec for rec in self._space(space_id).values()
            if memory_id in rec.linked_memory_ids and self._matches_filters(rec, filters)
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
        scope=u1.scope, memory_id="u1",
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
        scope=u1.scope, memory_id="u1",
    )

    assert result.deleted_count == 1
    assert result.updated_count == 0
    assert store.records(space_id_from_scope(u1.scope)) == []


def test_unlink_unknown_memory_id_is_noop(store: InMemoryEntityStore) -> None:
    linker = _linker(store)
    u1 = _make_unit("u1", "x", entities=["Alice"])
    linker.link_memories([u1])

    result = linker.unlink_memory(
        scope=u1.scope, memory_id="nonexistent",
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
        scope=u1.scope, memory_id="u1",
    )

    # Alice 关联 u1,u2 → 删 u1 后剩 u2 → UNLINK_UPDATE
    # Paris 只关联 u1 → 删 u1 后空 → DELETE
    # Berlin 只关联 u2 → 不受影响
    assert result.updated_count == 1
    assert result.deleted_count == 1


# ---------------------------------------------------------------------------
# unlink_memory 跨 user 隔离（检视意见回归）
# ---------------------------------------------------------------------------


def test_unlink_does_not_touch_other_user_entity(store: InMemoryEntityStore) -> None:
    # 检视意见回归：find_by_linked_memory_id 必须带 actor_id filter，unlink 只
    # 命中调用方 scope 所属的实体文档。构造同 space、不同 user、且 memory_id
    # 相同（"撞 id"）的极端场景——即便 id 撞了，删 user1 的记忆也不能动
    # user2 的实体关联（纵深防御：当前生产 unit.id 是 UUID4 不会撞，但隔离
    # 下沉到存储层后，任何 id 体系都安全）。
    scope_u1 = Scope(org="o1", user="alice", agent="a1", session="s1")
    scope_u2 = Scope(org="o1", user="bob", agent="a1", session="s1")
    # 两个 user 同 space（org 相同 → space_id 相同），memory_id 故意相同
    unit_a = _make_unit("colliding-id", "x", entities=["Acme"], scope=scope_u1)
    unit_b = _make_unit("colliding-id", "x", entities=["Acme"], scope=scope_u2)
    linker = _linker(store)
    linker.link_memories([unit_a, unit_b])

    # 同 space 下应有两条 Acme 文档（per-user 隔离：find_by_entity_text_hash
    # 带 actor_id filter，user 不同 → 各自 INSERT，不归并到同一条）
    recs = store.records(space_id_from_scope(scope_u1))
    assert len(recs) == 2
    actor_ids = sorted(r.filters.actor_id for r in recs)
    assert actor_ids == ["alice", "bob"]
    # 两条文档的 linked_memory_ids 都含 "colliding-id"
    assert all("colliding-id" in r.linked_memory_ids for r in recs)

    # 删 user=alice 的记忆：unlink 用 scope_u1，find_by_linked_memory_id 带
    # actor_id=alice filter，只命中 alice 的那条 → DELETE。bob 的那条必须不动。
    result = linker.unlink_memory(scope=scope_u1, memory_id="colliding-id")

    assert result.deleted_count == 1
    assert result.updated_count == 0
    recs_after = store.records(space_id_from_scope(scope_u1))
    assert len(recs_after) == 1
    survivor = recs_after[0]
    assert survivor.filters.actor_id == "bob"
    assert "colliding-id" in survivor.linked_memory_ids


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _hash(name: str) -> str:
    return hash_entity_text(EntityNormalizer.normalize(name))


# ---------------------------------------------------------------------------
# 查询失败不降级 INSERT（检视意见子点1 回归）
# ---------------------------------------------------------------------------


class _FailingLookupStore:
    """包装 InMemoryEntityStore，让 find_by_entity_text_hash 第 N 次调用抛异常。

    复现检视意见"精确查询失败后被当成实体不存在，继续 INSERT，可能制造重复
    实体"：原 buggy 代码 catch 异常后置 existing_by_hash={}，循环误判每个实体
    不存在 → 全 INSERT。若已存在同 hash 的 EntityRecord，会新建第二条（uuid4
    为 _id），召回侧 find_by_entity_text_hash 命中多条，打分失真。
    """

    def __init__(self, inner: InMemoryEntityStore) -> None:
        self._inner = inner
        self.lookup_count = 0
        self.fail_on_lookup: int | None = None  # 第几次调用抛异常

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def find_by_entity_text_hash(self, *args, **kwargs):
        self.lookup_count += 1
        if self.fail_on_lookup is not None and self.lookup_count == self.fail_on_lookup:
            raise RuntimeError("simulated lookup failure")
        return self._inner.find_by_entity_text_hash(*args, **kwargs)


def test_link_lookup_failure_does_not_create_duplicates(store: InMemoryEntityStore) -> None:
    """查询失败时不降级 INSERT：整组 abort 计 failed，不造重复实体文档。

    回归检视意见子点1。场景：
      第一次 write u1（含 Alice）→ 查询成功，INSERT 一条 Alice 文档
      第二次 write u2（含 Alice）→ 查询失败（模拟 ES 5xx/网络抖动）
        修复前：existing_by_hash={} → 循环误判 Alice 不存在 → INSERT 第二条
                → 同 hash 两条文档，召回侧打分翻倍
        修复后：整组 abort，failed_count=1，不 INSERT，不造重复
      第三次 write u3（含 Alice）→ 查询恢复，hash 命中第一条 → LINK（自愈）
    """
    failing = _FailingLookupStore(store)
    failing.fail_on_lookup = 2  # 第二次查询抛异常
    linker = _linker(failing)
    space_id = space_id_from_scope(_make_unit("u1", "x", entities=["Alice"]).scope)

    # 第一次：查询成功，INSERT 一条 Alice
    u1 = _make_unit("u1", "x", entities=["Alice"])
    r1 = linker.link_memories([u1])
    assert r1.inserted_count == 1
    assert store.records(space_id) != [] and len(store.records(space_id)) == 1

    # 第二次：查询失败，应 abort 不 INSERT
    u2 = _make_unit("u2", "x", entities=["Alice"])
    r2 = linker.link_memories([u2])
    assert r2.inserted_count == 0, "查询失败时不应 INSERT（避免造重复）"
    assert r2.updated_count == 0
    assert r2.failed_count == 1, "查询失败应整组计 failed"

    # 不造重复：仍只有一条 Alice 文档
    recs_after = store.records(space_id)
    assert len(recs_after) == 1, (
        f"查询失败不应造重复文档，实际 {len(recs_after)} 条"
    )
    # 第一条文档不变（仍只含 u1，u2 没被 LINK 进去——失败的组没有副作用）
    assert set(recs_after[0].linked_memory_ids) == {"u1"}

    # 第三次：查询恢复，hash 命中 → LINK（自愈语义验证）
    failing.fail_on_lookup = None
    u3 = _make_unit("u3", "x", entities=["Alice"])
    r3 = linker.link_memories([u3])
    assert r3.inserted_count == 0
    assert r3.updated_count == 1, "查询恢复后应 LINK 到已存文档（自愈）"
    assert r3.failed_count == 0
    recs_final = store.records(space_id)
    assert len(recs_final) == 1, "自愈后仍只有一条文档，不造重复"
    assert set(recs_final[0].linked_memory_ids) == {"u1", "u3"}


def test_link_lookup_failure_counts_all_entities_in_group(
    store: InMemoryEntityStore,
) -> None:
    """查询失败时整组每个实体都计 failed（不止触发失败的那条）。

    一个 group 含多个实体（Alice, Bob），查询失败时全组 abort，failed_count
    = 实体数（2），不部分 INSERT。
    """
    failing = _FailingLookupStore(store)
    failing.fail_on_lookup = 1  # 第一次查询就抛
    linker = _linker(failing)
    space_id = space_id_from_scope(_make_unit("u1", "x", entities=["Alice"]).scope)

    unit = _make_unit("u1", "x", entities=["Alice", "Bob"])
    result = linker.link_memories([unit])

    assert result.failed_count == 2, (
        f"整组 2 个实体都应计 failed，实际 {result.failed_count}"
    )
    assert result.inserted_count == 0
    assert result.updated_count == 0
    # 一个文档都没建（查询失败，不能假设不存在就 INSERT）
    assert store.records(space_id) == []


# ---------------------------------------------------------------------------
# build 失败可见不阻断（检视意见子点5 回归）
# ---------------------------------------------------------------------------


def test_build_swallows_failure_but_logs_error(
    store: InMemoryEntityStore, caplog
) -> None:
    """link_memories 抛异常时 build 不阻断 write，但 error 级别可见。

    回归检视意见子点5"异常被吞掉后没有 outbox、重试或可执行的重建入口"——
    原代码用 logger.warning 静默吞，失败完全不可见，运维无法感知 entity 索引
    stale。修复后：保留不阻断（entity 是增强层，fulltext/vector+真源已落盘，
    失败可自愈），但用 error 级别让失败可见、可告警。
    """
    linker = _linker(store)
    builder = EntityIndexBuilder(linker)
    unit = _make_unit("u1", "x", entities=["Alice"])

    # 让 link_memories 抛异常（模拟 ES bulk 整体不可达）
    def _boom(_units):
        raise RuntimeError("simulated backend down")
    linker.link_memories = _boom  # type: ignore[method-assign]

    import logging
    logging.getLogger("agent_memory").propagate = True
    with caplog.at_level(logging.ERROR, logger="jiuwen_memory.construction.index_builder_impl.entity_index_builder"):
        # 不抛——build 不阻断
        builder.build([unit])

    # error 级别日志可见（修复前是 warning，被 caplog.at_level(ERROR) 过滤掉）
    assert any("link_memories failed" in r.message for r in caplog.records), (
        "build 失败应 error 级别可见，实际无 error 日志"
    )


def test_build_logs_partial_failure_when_failed_count_nonzero(
    store: InMemoryEntityStore, caplog
) -> None:
    """link_memories 部分失败（failed_count>0，如查询超时 abort）时 error 可见。

    与上一测试的区别：上一测试 link_memories 整体抛异常；本测试 link_memories
    正常返回但 failed_count>0（查询超时整组 abort 的结果）。build 应 error 级别
    记录部分失败，带 failed_count 便于对账。
    """
    failing = _FailingLookupStore(store)
    failing.fail_on_lookup = 1  # 查询抛 → 整组 abort → failed_count=2
    linker = _linker(failing)
    builder = EntityIndexBuilder(linker)
    unit = _make_unit("u1", "x", entities=["Alice", "Bob"])

    import logging
    logging.getLogger("agent_memory").propagate = True
    with caplog.at_level(logging.ERROR, logger="jiuwen_memory.construction.index_builder_impl.entity_index_builder"):
        builder.build([unit])  # 不抛，返回 EntityLinkResult(failed_count=2)

    assert any("partial failure" in r.message and "failed=2" in r.message
               for r in caplog.records), (
        "部分失败应 error 级别带 failed_count 可见，实际无对应日志"
    )
