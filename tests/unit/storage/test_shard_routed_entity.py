# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 S4：实体 ``shard_routed`` 包装层（shared / split）与增量解除关联。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import (
    BackendError,
    UnsupportedStorageCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.entity import (
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntityStoreFilters,
    hash_entity_text,
)
from jiuwen_memory.storage.entity_impl.in_memory_entity_store import InMemoryEntityStore
from jiuwen_memory.storage.entity_impl.shard_routed_entity import (
    ShardRoutedEntityStore,
    logical_id_of,
    physical_id,
)
from jiuwen_memory.storage.shard_routing import RuleBasedShardResolver, ShardLocation
from tests.support.whitebox import private

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")
SHARDS = ("working", "profile", "main")
FILTERS = EntityStoreFilters(actor_id="u1")


class DictLocator:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def locate(self, scope: Scope, unit_ids: list[str]) -> ShardLocation:
        found = {uid: self.mapping[uid] for uid in unit_ids if uid in self.mapping}
        return ShardLocation(found=found, unlocated=frozenset(set(unit_ids) - set(found)))


def resolver() -> RuleBasedShardResolver:
    return RuleBasedShardResolver(
        shards=list(SHARDS), default_shard="main", rules=[]
    )


def record(logical_id: str, linked: tuple[str, ...]) -> EntityRecord:
    return EntityRecord(
        id=logical_id,
        space_id="acme",
        entity_text="Alice",
        entity_type="PROPER",
        linked_memory_ids=linked,
        filters=FILTERS,
        entity_text_hash=hash_entity_text("alice"),
        embedding=[1.0, 0.0],
    )


def op(op_type: EntityOpType, *, record_id: str | None = None, **kwargs) -> EntityOperation:
    return EntityOperation(type=op_type, record_id=record_id, **kwargs)


def shared_store() -> tuple[ShardRoutedEntityStore, InMemoryEntityStore]:
    inner = InMemoryEntityStore()
    routed = ShardRoutedEntityStore(
        resolver=resolver(),
        locator=DictLocator(
            {"u1": "profile", "u1b": "profile", "u2": "working", "u3": "main"}
        ),
        mode="shared",
        inner=inner,
    )
    return routed, inner


def split_store() -> tuple[ShardRoutedEntityStore, dict[str, InMemoryEntityStore]]:
    stores = {name: InMemoryEntityStore() for name in SHARDS}
    routed = ShardRoutedEntityStore(
        resolver=resolver(),
        locator=DictLocator(
            {"u1": "profile", "u1b": "profile", "u2": "working", "u3": "main"}
        ),
        mode="split",
        stores=stores,
    )
    return routed, stores


class TestSharedEntity:
    @staticmethod
    def test_insert_link_unlink_delete_keep_grouped_view() -> None:
        """TC-S4-1：三字段一致、UNLINK 移除、空记录不返回。"""
        routed, inner = shared_store()
        routed.ensure_index()
        result = routed.execute_operations(
            SCOPE,
            [op(EntityOpType.INSERT, record=record("e1", ("u1", "u2")))],
        )
        assert result.successful_ids == ["e1"]

        found = routed.find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )
        assert [item.linked_memory_ids for item in found] == [("u1", "u2")]

        routed.execute_operations(
            SCOPE, [op(EntityOpType.UNLINK, record_id="e1", unlink_memory_ids=("u1",))]
        )
        found = routed.find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )
        assert found[0].linked_memory_ids == ("u2",)

        routed.execute_operations(SCOPE, [op(EntityOpType.DELETE, record_id="e1")])
        assert routed.find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        ) == []

    @staticmethod
    def test_shards_filter_returns_selected_groups() -> None:
        """TC-S4-2：shared 模式 shards 过滤只返回选中分组的关联。"""
        routed, _ = shared_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1", "u2")))]
        )
        found = routed.find_by_entity_text_hash(
            SCOPE,
            (hash_entity_text("alice"),),
            filters=FILTERS,
            shards=frozenset({"working"}),
        )
        assert found[0].linked_memory_ids == ("u2",)
        assert routed.find_by_entity_text_hash(
            SCOPE,
            (hash_entity_text("alice"),),
            filters=FILTERS,
            shards=frozenset({"main"}),
        ) == []

    @staticmethod
    def test_unlink_last_link_then_delete_if_empty() -> None:
        """TC-S4-4/5：UNLINK 后为空不返回；DELETE_IF_EMPTY 删除空记录。"""
        routed, inner = shared_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1",)))]
        )
        routed.execute_operations(
            SCOPE,
            [
                op(EntityOpType.UNLINK, record_id="e1", unlink_memory_ids=("u1",)),
                op(EntityOpType.DELETE_IF_EMPTY, record_id="e1"),
            ],
        )
        assert routed.find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        ) == []
        # 已删除：对非空记录不生效的路径另测
        routed.execute_operations(
            SCOPE,
            [
                op(EntityOpType.INSERT, record=record("e2", ("u1",))),
                op(EntityOpType.DELETE_IF_EMPTY, record_id="e2"),
            ],
        )
        assert routed.find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )

    @staticmethod
    def test_concurrent_link_and_unlink_do_not_lose_links() -> None:
        """TC-S4-3：并发增量 LINK / UNLINK 不丢关联。"""
        import threading

        routed, _ = shared_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1", "u2")))]
        )
        barrier = threading.Barrier(2)

        def link() -> None:
            barrier.wait()
            routed.execute_operations(
                SCOPE, [op(EntityOpType.LINK, record_id="e1", link_memory_ids=("u3",))]
            )

        def unlink() -> None:
            barrier.wait()
            routed.execute_operations(
                SCOPE,
                [op(EntityOpType.UNLINK, record_id="e1", unlink_memory_ids=("u2",))],
            )

        threads = [threading.Thread(target=link), threading.Thread(target=unlink)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        found = routed.find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )
        assert set(found[0].linked_memory_ids) == {"u1", "u3"}

    @staticmethod
    def test_unlocated_link_fails_only_that_operation() -> None:
        """TC-S4：单条定位失败只失败该条。"""
        routed, _ = shared_store()
        result = routed.execute_operations(
            SCOPE,
            [
                op(EntityOpType.INSERT, record=record("e1", ("u1",))),
                op(EntityOpType.INSERT, record=record("e2", ("ghost",))),
            ],
        )
        assert result.successful_ids == ["e1"]
        assert result.failed_ids == ["e2"]

    @staticmethod
    def test_inner_missing_capability_rejected() -> None:
        """TC-S4-10：内层未声明 atomic_unlink / grouped_links 时装配失败。"""

        class NoCapabilityStore(InMemoryEntityStore):
            @staticmethod
            def capabilities():
                return frozenset()

        with pytest.raises(UnsupportedStorageCapabilityError):
            ShardRoutedEntityStore(
                resolver=resolver(),
                locator=DictLocator({}),
                mode="shared",
                inner=NoCapabilityStore(),
            )


class TestSplitEntity:
    @staticmethod
    def test_insert_creates_deterministic_physical_docs() -> None:
        """TC-S4-6：split 物理 id = 逻辑 id@分片，对外 id 为逻辑 id。"""
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1", "u2")))]
        )
        assert stores["profile"].find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )
        physical = physical_id("e1", "profile")
        assert logical_id_of(physical) == "e1"
        found = routed.find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )
        assert [item.id for item in found] == ["e1"]
        assert set(found[0].linked_memory_ids) == {"u1", "u2"}

    @staticmethod
    def test_link_same_shard_updates_existing_doc() -> None:
        """P0：同分片追加关联必须走 LINK，不能误发 INSERT 冲突。"""
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1",)))]
        )
        result = routed.execute_operations(
            SCOPE,
            [
                EntityOperation(
                    type=EntityOpType.LINK,
                    record_id="e1",
                    link_memory_ids=("u1b",),
                    record=record("e1", ("u1",)),
                )
            ],
        )
        assert not result.failed_ids
        # u1b 不在 locator 里，应只失败不出 INSERT；这里用已配置 id 重测
        assert stores["profile"].find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )

    @staticmethod
    def test_link_does_not_mistake_other_entity_link() -> None:
        """P0：id 已被别的实体关联时，不能据此判断目标物理文档存在。"""
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE,
            [
                op(EntityOpType.INSERT, record=record("e1", ("u1",))),
                op(EntityOpType.INSERT, record=record("e2", ("u2",))),
            ],
        )
        result = routed.execute_operations(
            SCOPE,
            [
                EntityOperation(
                    type=EntityOpType.LINK,
                    record_id="e1",
                    link_memory_ids=("u2",),
                    record=record("e1", ("u1",)),
                )
            ],
        )
        assert not result.failed_ids
        found = routed.find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )
        assert len(found) == 2
        assert "u2" in next(item for item in found if item.id == "e1").linked_memory_ids

    @staticmethod
    def test_split_results_expose_logical_ids() -> None:
        """P2：split 的批量结果不得暴露 `{id}@{shard}` 物理 id。"""
        routed, _ = split_store()
        result = routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1", "u2")))]
        )
        assert result.successful_ids == ["e1"]  # 两个物理文档去重后仍是逻辑 id
        assert result.failed_ids == []

    @staticmethod
    def test_link_without_catalog_uses_operation_template() -> None:
        """P0：新进程 / 无进程内目录时，LINK 仍能按操作携带的模板 upsert。"""
        routed, stores = split_store()
        # 直接构造操作，不经过 INSERT（模拟目录为空的新进程）
        template = record("e1", ())
        result = routed.execute_operations(
            SCOPE,
            [
                EntityOperation(
                    type=EntityOpType.LINK,
                    record_id="e1",
                    link_memory_ids=("u1",),
                    record=template,
                )
            ],
        )
        assert not result.failed_ids
        assert stores["profile"].find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )

    @staticmethod
    def test_link_without_record_appends_to_existing_physical_doc() -> None:
        """P1：契约里 LINK 的 record 可选；物理文档已存在时不带 record 也要成功追加。"""
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1",)))]
        )
        result = routed.execute_operations(
            SCOPE,
            [op(EntityOpType.LINK, record_id="e1", link_memory_ids=("u1b",))],
        )
        assert result.successful_ids == ["e1"], result
        assert result.failed_ids == [], result
        docs = stores["profile"].find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )
        assert set(docs[0].linked_memory_ids) == {"u1", "u1b"}

    @staticmethod
    def test_link_without_record_to_new_shard_fails_without_writing() -> None:
        """P1：首次关联到新分片且不带 record 时按条失败，不在该分片凭空建文档。"""
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1",)))]
        )
        result = routed.execute_operations(
            SCOPE,
            [op(EntityOpType.LINK, record_id="e1", link_memory_ids=("u2",))],
        )
        assert result.failed_ids == ["e1"], result
        assert result.successful_ids == [], result
        assert not stores["working"].find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )

    @staticmethod
    def test_link_partial_cross_shard_reports_failure_and_retry_is_idempotent() -> None:
        """P1：跨分片 LINK 一份成功一份失败时整条报失败；补 record 重试后收敛。"""
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1",)))]
        )
        partial = routed.execute_operations(
            SCOPE,
            [op(EntityOpType.LINK, record_id="e1", link_memory_ids=("u1b", "u2"))],
        )
        assert partial.failed_ids == ["e1"] and partial.successful_ids == [], partial
        retry = routed.execute_operations(
            SCOPE,
            [
                EntityOperation(
                    type=EntityOpType.LINK,
                    record_id="e1",
                    link_memory_ids=("u1b", "u2"),
                    record=record("e1", ("u1",)),
                )
            ],
        )
        assert retry.successful_ids == ["e1"] and not retry.failed_ids, retry
        found = routed.find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )
        assert set(found[0].linked_memory_ids) == {"u1", "u1b", "u2"}

    @staticmethod
    def test_link_creates_missing_physical_doc() -> None:
        """TC-S4-7：跨分片 LINK 在缺失分片创建物理文档，按逻辑 id 聚合。"""
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1",)))]
        )
        result = routed.execute_operations(
            SCOPE,
            [
                EntityOperation(
                    type=EntityOpType.LINK,
                    record_id="e1",
                    link_memory_ids=("u3",),
                    record=record("e1", ("u1",)),
                )
            ],
        )
        assert result.successful_ids == ["e1"]
        found = routed.find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )
        assert set(found[0].linked_memory_ids) == {"u1", "u3"}
        assert stores["main"].find_by_linked_memory_id(
            SCOPE, "u3", filters=FILTERS
        )

    @staticmethod
    def test_unlink_without_placement_fans_out() -> None:
        """TC-S4-9：记忆无归属时 UNLINK 仍幂等完成。"""
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1", "u2")))]
        )
        result = routed.execute_operations(
            SCOPE, [op(EntityOpType.UNLINK, record_id="e1", unlink_memory_ids=("u1",))]
        )
        assert not result.failed_ids
        for store in stores.values():
            for item in store.find_by_entity_text_hash(
                SCOPE, (hash_entity_text("alice"),), filters=FILTERS
            ):
                assert "u1" not in item.linked_memory_ids

    @staticmethod
    def test_delete_removes_all_physical_docs() -> None:
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1", "u2")))]
        )
        routed.execute_operations(SCOPE, [op(EntityOpType.DELETE, record_id="e1")])
        for store in stores.values():
            assert store.find_by_entity_text_hash(
                SCOPE, (hash_entity_text("alice"),), filters=FILTERS
            ) == []

    @staticmethod
    def test_insert_retry_is_idempotent() -> None:
        """TC-S4-8：INSERT 重试不产生重复物理文档。"""
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1",)))]
        )
        result = routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1",)))]
        )
        assert not result.failed_ids  # 已存在视为已完成
        docs = stores["profile"].find_by_entity_text_hash(
            SCOPE, (hash_entity_text("alice"),), filters=FILTERS
        )
        assert len(docs) == 1

    @staticmethod
    def test_search_aggregates_by_logical_id() -> None:
        routed, _ = split_store()
        routed.execute_operations(
            SCOPE, [op(EntityOpType.INSERT, record=record("e1", ("u1", "u2")))]
        )
        hits = routed.search(SCOPE, [1.0, 0.0], top_k=5, filters=FILTERS)
        assert [hit.record.id for hit in hits] == ["e1"]

    @staticmethod
    def test_unknown_shard_name_rejected() -> None:
        routed, _ = split_store()
        with pytest.raises(ValidationError):
            routed.find_by_entity_text_hash(
                SCOPE, ("h",), filters=FILTERS, shards=frozenset({"ghost"})
            )


class TestEntityRelocation:
    """F09 D16：实体包装层的搬迁 / 清理（split 模式）。"""

    @staticmethod
    def test_split_relocate_copies_then_cleanup_removes_source() -> None:
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE,
            [
                op(
                    EntityOpType.LINK,
                    record_id="e1",
                    link_memory_ids=("u1", "u2"),
                    record=record("e1", ("u1", "u2")),
                )
            ],
        )

        routed.relocate_unit(SCOPE, "u1", "profile", "working")
        moved = stores["working"].find_by_linked_memory_id(
            SCOPE, "u1", filters=FILTERS
        )
        assert [physical_id("e1", "working")] == [item.id for item in moved]

        routed.cleanup_unit(SCOPE, "u1", "profile")
        assert stores["profile"].find_by_linked_memory_id(
            SCOPE, "u1", filters=FILTERS
        ) == []
        # 其它关联（u2）仍保留在它所在的分片，未被搬迁清理误删
        assert stores["working"].find_by_linked_memory_id(
            SCOPE, "u2", filters=FILTERS
        )

    @staticmethod
    def test_split_cleanup_deletes_source_document_left_without_links() -> None:
        """源分片的实体文档只关联被搬走的记忆时，清理后不残留空文档。"""
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE,
            [
                op(
                    EntityOpType.LINK,
                    record_id="e1",
                    link_memory_ids=("u1",),
                    record=record("e1", ("u1",)),
                )
            ],
        )
        assert private(stores["profile"], "_records"), "fixture should create the source document"

        routed.relocate_unit(SCOPE, "u1", "profile", "working")
        routed.cleanup_unit(SCOPE, "u1", "profile")

        assert private(stores["profile"], "_records") == {}, "empty source document must be deleted"
        assert stores["working"].find_by_linked_memory_id(SCOPE, "u1", filters=FILTERS)


class _FailingLinkStore(InMemoryEntityStore):
    """按条报告 LINK 失败（不抛异常），模拟后端部分失败。"""

    @staticmethod
    def execute_operations(scope, operations):
        from jiuwen_memory.common.type_def.entity import EntityBatchResult

        if any(item.type is EntityOpType.LINK for item in operations):
            return EntityBatchResult(
                successful_ids=[],
                failed_ids=[item.record_id or "" for item in operations],
            )
        return super().execute_operations(scope, operations)


class TestEntityRelocationSafety:
    """评审 P1-2：搬迁检查批量结果；共享模式分组迁移可恢复。"""

    @staticmethod
    def test_split_relocate_raises_when_backend_reports_failed_link() -> None:
        routed, stores = split_store()
        routed.execute_operations(
            SCOPE,
            [op(EntityOpType.LINK, record_id="e1", link_memory_ids=("u1",),
                record=record("e1", ("u1",)))],
        )
        failing = _FailingLinkStore()
        private(routed, "_stores")["working"] = failing

        with pytest.raises(BackendError):
            routed.relocate_unit(SCOPE, "u1", "profile", "working")

    @staticmethod
    def test_shared_relocate_raises_when_backend_reports_failed_link() -> None:
        inner = _FailingLinkStore()
        routed = ShardRoutedEntityStore(
            resolver=resolver(),
            locator=DictLocator({"u1": "profile"}),
            mode="shared",
            inner=inner,
        )
        InMemoryEntityStore.execute_operations(
            inner,
            SCOPE,
            [op(EntityOpType.INSERT, record=record("e1", ("u1",)),
                memory_shards={"u1": "profile"})],
        )
        with pytest.raises(BackendError):
            routed.relocate_unit(SCOPE, "u1", "profile", "working")

    @staticmethod
    def test_shared_relocation_keeps_unit_linked_at_every_step() -> None:
        routed, inner = shared_store()
        routed.execute_operations(
            SCOPE,
            [op(EntityOpType.LINK, record_id="e1", link_memory_ids=("u1", "u2"),
                record=record("e1", ("u1", "u2")))],
        )

        routed.relocate_unit(SCOPE, "u1", "profile", "working")
        # 「崩溃」在复制之后：关联仍可经反查找到，两个分组都有它
        found = inner.find_by_linked_memory_id(SCOPE, "u1", filters=FILTERS)
        assert [item.id for item in found] == ["e1"]
        stored = private(inner, "_records")[next(iter(private(inner, "_records")))]
        assert "u1" in stored.linked_by_shard["profile"]
        assert "u1" in stored.linked_by_shard["working"]

        # 回滚：清理目标分组，源分组与其他记忆不受影响
        routed.cleanup_unit(SCOPE, "u1", "working")
        assert stored.linked_by_shard.get("working", []) == ["u2"]
        assert stored.linked_by_shard["profile"] == ["u1"]

        # 正常完成：重新复制后清理源分组
        routed.relocate_unit(SCOPE, "u1", "profile", "working")
        routed.cleanup_unit(SCOPE, "u1", "profile")
        assert "profile" not in stored.linked_by_shard
        assert sorted(stored.linked_by_shard["working"]) == ["u1", "u2"]
        assert "u1" in stored.all_ids()


class _RecordingEntityStore(InMemoryEntityStore):
    def __init__(self) -> None:
        super().__init__()
        self.complete_flags: list[bool] = []

    def find_by_linked_memory_id(
        self, scope, memory_id, *, filters, shards=None, complete=False
    ):
        self.complete_flags.append(complete)
        return super().find_by_linked_memory_id(
            scope, memory_id, filters=filters, shards=shards, complete=complete
        )


class TestEntityRelocationCompleteness:
    """D16 修复：搬迁必须请求完整枚举，不能把限量查询当成完整集合。"""

    @staticmethod
    def test_relocation_requests_complete_enumeration() -> None:
        inner = _RecordingEntityStore()
        routed = ShardRoutedEntityStore(
            resolver=resolver(),
            locator=DictLocator({"u1": "working"}),
            mode="shared",
            inner=inner,
        )
        routed.execute_operations(
            SCOPE,
            [
                op(
                    EntityOpType.LINK,
                    record_id="e1",
                    link_memory_ids=("u1",),
                    record=record("e1", ("u1",)),
                )
            ],
        )

        routed.relocate_unit(SCOPE, "u1", "working", "profile")

        assert inner.complete_flags == [True]


class _OldSignatureEntityStore(InMemoryEntityStore):
    """模拟未实现 ``complete`` 参数的第三方实体插件（普通反查仍可用）。"""

    @property
    def supports_complete_linked_enumeration(self) -> bool:
        return False

    def find_by_linked_memory_id(
        self, scope, memory_id, *, filters, shards=None
    ):
        return super().find_by_linked_memory_id(
            scope, memory_id, filters=filters, shards=shards
        )


class TestThirdPartyCompatibility:
    @staticmethod
    def test_normal_lookup_does_not_pass_complete_to_plugins() -> None:
        inner = _OldSignatureEntityStore()
        routed = ShardRoutedEntityStore(
            resolver=resolver(),
            locator=DictLocator({"u1": "working"}),
            mode="shared",
            inner=inner,
        )
        routed.execute_operations(
            SCOPE,
            [
                op(
                    EntityOpType.LINK,
                    record_id="e1",
                    link_memory_ids=("u1",),
                    record=record("e1", ("u1",)),
                )
            ],
        )

        from jiuwen_memory.storage.capability import StoreCapability

        # 内层不能完整枚举时，包装层不能声明可搬迁（否则第一次搬迁才失败）。
        assert StoreCapability.SHARD_RELOCATABLE not in routed.capabilities()

        # 未开启搬迁的普通反查不能因为多传 complete=False 而报错。
        assert routed.find_by_linked_memory_id(
            SCOPE, "u1", filters=FILTERS
        )

        with pytest.raises(
            UnsupportedStorageCapabilityError, match="complete enumeration"
        ):
            routed.relocate_unit(SCOPE, "u1", "working", "profile")
