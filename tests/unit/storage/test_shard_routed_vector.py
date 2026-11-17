# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 S3：向量 / 全文 ``shard_routed`` 包装层。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import (
    ConflictError,
    UnsupportedStorageCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
    WhitespaceTokenizer,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.capability import StoreCapability
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import (
    InMemoryFulltextStore,
)
from jiuwen_memory.storage.fulltext_impl.shard_routed_fulltext import (
    ShardRoutedFulltextStore,
)
from jiuwen_memory.storage.shard_routing import RuleBasedShardResolver, ShardLocation
from jiuwen_memory.storage.shard_search import STORAGE_SHARD_FIELD, STORAGE_SHARD_NEXT_FIELD
from jiuwen_memory.storage.types import Document, TextQuery, VectorQuery, VectorRecord
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore
from jiuwen_memory.storage.vector_impl.shard_routed_vector import ShardRoutedVectorStore

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")
SHARDS = ("working", "profile", "main")


class DictLocator:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def locate(self, scope: Scope, unit_ids: list[str]) -> ShardLocation:
        found = {uid: self.mapping[uid] for uid in unit_ids if uid in self.mapping}
        return ShardLocation(
            found=found, unlocated=frozenset(set(unit_ids) - set(found))
        )


class SpyVectorStore(InMemoryVectorStore):
    def __init__(self) -> None:
        super().__init__()
        self.searches = 0

    def search(self, scope, query):
        self.searches += 1
        return super().search(scope, query)


class SpyFulltextStore(InMemoryFulltextStore):
    def __init__(self) -> None:
        super().__init__(WhitespaceTokenizer())
        self.searches = 0

    def search(self, scope, query):
        self.searches += 1
        return super().search(scope, query)


class ReverseScoreVectorStore(InMemoryVectorStore):
    @staticmethod
    def score_higher_is_better() -> bool:
        return False


def resolver() -> RuleBasedShardResolver:
    return RuleBasedShardResolver(
        shards=list(SHARDS),
        default_shard="main",
        rules=[],
    )


def vector_record(record_id: str, unit_id: str) -> VectorRecord:
    return VectorRecord(
        id=record_id, vector=[1.0, 0.0], metadata={"unit_id": unit_id}
    )


def split_vector(locator=None) -> tuple[ShardRoutedVectorStore, dict[str, SpyVectorStore]]:
    stores = {name: SpyVectorStore() for name in SHARDS}
    routed = ShardRoutedVectorStore(
        resolver=resolver(),
        locator=locator or DictLocator({"u1": "profile", "u2": "working"}),
        mode="split",
        stores=stores,
    )
    return routed, stores


class TestSplitVector:
    @staticmethod
    def test_insert_routes_via_locator() -> None:
        """TC-S3-1：split 写入跟随归属，不按 metadata 自行判定。"""
        routed, stores = split_vector()
        routed.insert(SCOPE, [vector_record("u1-chunk", "u1")])
        assert stores["profile"].get(SCOPE, ["u1-chunk"])
        assert not stores["working"].get(SCOPE, ["u1-chunk"])

    @staticmethod
    def test_unlocated_record_fails_without_default_shard() -> None:
        """TC-S3-2：未定位写入失败，不落默认分片。"""
        routed, stores = split_vector()
        with pytest.raises(ValidationError):
            routed.insert(SCOPE, [vector_record("ghost-chunk", "ghost")])
        for store in stores.values():
            assert not store.get(SCOPE, ["ghost-chunk"])

    @staticmethod
    def test_batch_partial_failure_keeps_successful_group() -> None:
        """TC-S3-4：成功组不回滚。"""
        routed, stores = split_vector()
        with pytest.raises(ValidationError):
            routed.insert(
                SCOPE,
                [vector_record("u1-chunk", "u1"), vector_record("ghost-chunk", "ghost")],
            )
        assert stores["profile"].get(SCOPE, ["u1-chunk"])

    @staticmethod
    @pytest.mark.parametrize(
        "record_id,unit_id",
        [
            ("other-id", "u1"),
            ("u-other-chunk", "u1"),
            ("u1chunk", "u1"),
        ],
    )
    def test_record_id_must_derive_from_unit_id(record_id: str, unit_id: str) -> None:
        """TC-S3-3：记录 id 与 unit_id 绑定校验。"""
        routed, _ = split_vector()
        with pytest.raises(ValidationError):
            routed.insert(SCOPE, [vector_record(record_id, unit_id)])

    @staticmethod
    def test_get_fans_out_delete_fans_out() -> None:
        """TC-S3-5：按 id 的 get / delete 扇出全部分片。"""
        routed, stores = split_vector()
        stores["profile"].insert(SCOPE, [vector_record("u1-chunk", "u1")])
        stores["main"].insert(SCOPE, [vector_record("orphan-chunk", "orphan")])
        assert [r.id for r in routed.get(SCOPE, ["u1-chunk", "orphan-chunk"])] == [
            "u1-chunk",
            "orphan-chunk",
        ]
        routed.delete(SCOPE, ["u1-chunk", "orphan-chunk"])
        assert not stores["profile"].get(SCOPE, ["u1-chunk"])
        assert not stores["main"].get(SCOPE, ["orphan-chunk"])

    @staticmethod
    def test_search_only_touches_selected_shards() -> None:
        """TC-S3-6：带 shards 只访问选中分片。"""
        routed, stores = split_vector()
        stores["profile"].insert(SCOPE, [vector_record("u1-chunk", "u1")])
        stores["working"].insert(SCOPE, [vector_record("u2-chunk", "u2")])
        hits = routed.search(
            SCOPE,
            VectorQuery(vector=[1.0, 0.0], top_k=5, extensions={"shards": ["working"]}),
        )
        assert [hit.id for hit in hits] == ["u2-chunk"]
        assert stores["profile"].searches == 0
        assert stores["working"].searches == 1

    @staticmethod
    def test_search_all_merges_and_truncates() -> None:
        routed, stores = split_vector()
        stores["profile"].insert(SCOPE, [vector_record("u1-chunk", "u1")])
        stores["working"].insert(SCOPE, [vector_record("u2-chunk", "u2")])
        hits = routed.search(SCOPE, VectorQuery(vector=[1.0, 0.0], top_k=1))
        assert len(hits) == 1

    @staticmethod
    def test_recall_merges_payload() -> None:
        routed, stores = split_vector()
        stores["profile"].insert(SCOPE, [vector_record("u1-chunk", "u1")])
        stores["working"].insert(SCOPE, [vector_record("u2-chunk", "u2")])
        hits = routed.recall(
            SCOPE, VectorQuery(vector=[1.0, 0.0], top_k=5), ["metadata"]
        )
        assert {hit.id for hit in hits} == {"u1-chunk", "u2-chunk"}
        assert all(hit.metadata.get("unit_id") for hit in hits)

    @staticmethod
    def test_inconsistent_score_direction_rejected() -> None:
        """TC-S3-9：分数方向不一致启动失败。"""
        stores = {name: SpyVectorStore() for name in SHARDS}
        stores["main"] = ReverseScoreVectorStore()
        with pytest.raises(ValidationError):
            ShardRoutedVectorStore(
                resolver=resolver(),
                locator=DictLocator({}),
                mode="split",
                stores=stores,
            )


class TestSharedVector:
    @staticmethod
    def test_shared_stamps_storage_shard_and_overrides_input() -> None:
        """TC-S3-7：shared 写入带 storage_shard，覆盖调用方同名输入。"""
        inner = SpyVectorStore()
        routed = ShardRoutedVectorStore(
            resolver=resolver(),
            locator=DictLocator({"u1": "profile"}),
            mode="shared",
            inner=inner,
        )
        record = VectorRecord(
            id="u1-chunk",
            vector=[1.0, 0.0],
            metadata={"unit_id": "u1", STORAGE_SHARD_FIELD: "forged"},
        )
        routed.insert(SCOPE, [record])
        stored = inner.get(SCOPE, ["u1-chunk"])[0]
        assert stored.metadata[STORAGE_SHARD_FIELD] == "profile"

    @staticmethod
    def test_shared_search_prefilters_before_top_k() -> None:
        """TC-S3-6 / D8：共享索引分片过滤在截断前下推。"""
        inner = SpyVectorStore()
        routed = ShardRoutedVectorStore(
            resolver=resolver(),
            locator=DictLocator({"u1": "profile", "u2": "working"}),
            mode="shared",
            inner=inner,
        )
        routed.insert(
            SCOPE,
            [vector_record("u1-chunk", "u1"), vector_record("u2-chunk", "u2")],
        )
        hits = routed.search(
            SCOPE,
            VectorQuery(vector=[1.0, 0.0], top_k=1, extensions={"shards": ["working"]}),
        )
        assert [hit.id for hit in hits] == ["u2-chunk"]

    @staticmethod
    def test_shared_inner_requires_prefilter_capability() -> None:
        """TC-S3-8：内层未声明前置过滤能力时装配失败。"""

        class NoPrefilterStore(InMemoryVectorStore):
            @staticmethod
            def capabilities():
                return frozenset()

        with pytest.raises(UnsupportedStorageCapabilityError):
            ShardRoutedVectorStore(
                resolver=resolver(),
                locator=DictLocator({}),
                mode="shared",
                inner=NoPrefilterStore(),
            )

    @staticmethod
    def test_recall_unsupported_inner() -> None:
        """P1：内层不支持 recall 时必须保留 NotImplementedError 以触发回退。"""

        class NoRecallStore(InMemoryVectorStore):
            @staticmethod
            def recall(*args, **kwargs):
                raise NotImplementedError

            @staticmethod
            def capabilities():
                return frozenset({StoreCapability.SHARD_PREFILTER})

        inner = NoRecallStore()
        routed = ShardRoutedVectorStore(
            resolver=resolver(),
            locator=DictLocator({"u1": "profile"}),
            mode="shared",
            inner=inner,
        )
        with pytest.raises(NotImplementedError):
            routed.recall(SCOPE, VectorQuery(vector=[1.0, 0.0]))


class TestSplitFulltext:
    @staticmethod
    def test_insert_and_search_selected_shard() -> None:
        """TC-S3-1/6：split 全文写入与按分片检索。"""
        stores = {name: SpyFulltextStore() for name in SHARDS}
        routed = ShardRoutedFulltextStore(
            resolver=resolver(),
            locator=DictLocator({"u1": "profile", "u2": "working"}),
            mode="split",
            stores=stores,
        )
        routed.insert(
            SCOPE,
            [
                Document("u1", "alpha beta", {"unit_id": "u1"}),
                Document("u2", "alpha gamma", {"unit_id": "u2"}),
            ],
        )
        hits = routed.search(
            SCOPE,
            TextQuery(text="alpha", top_k=5, extensions={"shards": ["profile"]}),
        )
        assert [hit.id for hit in hits] == ["u1"]
        assert stores["working"].searches == 0

    @staticmethod
    def test_fulltext_binding_validation() -> None:
        stores = {name: SpyFulltextStore() for name in SHARDS}
        routed = ShardRoutedFulltextStore(
            resolver=resolver(),
            locator=DictLocator({"u1": "profile"}),
            mode="split",
            stores=stores,
        )
        with pytest.raises(ValidationError):
            routed.insert(SCOPE, [Document("u1", "text", {"unit_id": "other"})])

    @staticmethod
    def test_fulltext_update_does_not_move_shard() -> None:
        stores = {name: SpyFulltextStore() for name in SHARDS}
        routed = ShardRoutedFulltextStore(
            resolver=resolver(),
            locator=DictLocator({"u1": "profile"}),
            mode="split",
            stores=stores,
        )
        routed.insert(SCOPE, [Document("u1", "alpha", {"unit_id": "u1"})])
        routed.update(
            SCOPE,
            [Document("u1", "alpha beta", {"unit_id": "u1", "tier": "working"})],
        )
        assert stores["profile"].get(SCOPE, ["u1"])
        assert not stores["working"].get(SCOPE, ["u1"])

    @staticmethod
    def test_shared_fulltext_prefilter() -> None:
        inner = SpyFulltextStore()
        routed = ShardRoutedFulltextStore(
            resolver=resolver(),
            locator=DictLocator({"u1": "profile", "u2": "working"}),
            mode="shared",
            inner=inner,
        )
        routed.insert(
            SCOPE,
            [
                Document("u1", "alpha", {"unit_id": "u1"}),
                Document("u2", "alpha", {"unit_id": "u2"}),
            ],
        )
        hits = routed.search(
            SCOPE,
            TextQuery(text="alpha", top_k=1, extensions={"shards": ["working"]}),
        )
        assert [hit.id for hit in hits] == ["u2"]


def test_shards_extension_validation_vector() -> None:
    routed, _ = split_vector()
    for value in ([], None, "profile", ["ghost"]):
        with pytest.raises(ValidationError):
            routed.search(
                SCOPE, VectorQuery(vector=[1.0], top_k=1, extensions={"shards": value})
            )


def test_write_does_not_delete_other_shard_records() -> None:
    """TC-S3-10：写入新分片不得清理其它分片的同 id 记录（旧任务恢复场景）。"""
    stores = {name: SpyVectorStore() for name in SHARDS}
    locator = DictLocator({"u1": "profile"})
    routed = ShardRoutedVectorStore(
        resolver=resolver(), locator=locator, mode="split", stores=stores
    )
    routed.insert(SCOPE, [vector_record("u1-chunk", "u1")])
    locator.mapping["u1"] = "working"
    routed.insert(SCOPE, [vector_record("u1-chunk", "u1")])
    assert stores["profile"].get(SCOPE, ["u1-chunk"])  # 未被盲删
    assert stores["working"].get(SCOPE, ["u1-chunk"])


def test_batch_failure_carries_failed_ids() -> None:
    """P2：批量写失败必须带回失败列表，调用方可按 id 重试。"""
    routed, _ = split_vector()
    with pytest.raises(ValidationError) as exc:
        routed.insert(
            SCOPE,
            [vector_record("u1-chunk", "u1"), vector_record("ghost-chunk", "ghost")],
        )
    assert getattr(exc.value, "failed_ids", None) == ["ghost-chunk"]


def test_inconsistent_embedding_config_rejected() -> None:
    """P2：分片间 embedding 配置不一致必须在装配期拒绝。"""
    from jiuwen_memory.config.context import AssemblyContext
    from jiuwen_memory.storage.vector import VectorProducer

    with pytest.raises(ValidationError):
        VectorProducer.build(
            "shard_routed",
            {
                "resolver": "default",
                "mode": "split",
                "table_template": {
                    "target": "memory",
                    "params": {"table": "v_{shard}", "dim": 8},
                },
                "tables": {
                    "working": {
                        "target": "memory",
                        "params": {"table": "v_working", "dim": 16},
                    }
                },
            },
            AssemblyContext.from_dict(
                {
                    "shard_resolver": {
                        "default": {
                            "target": "rule_based",
                            "params": {
                                "shards": ["working", "profile"],
                                "default_shard": "working",
                                "rules": [],
                            },
                        }
                    }
                }
            ),
        )


def test_duplicate_insert_conflict_propagates() -> None:
    routed, _ = split_vector()
    routed.insert(SCOPE, [vector_record("u1-chunk", "u1")])
    with pytest.raises(ConflictError):
        routed.insert(SCOPE, [vector_record("u1-chunk", "u1")])


class TestSharedRelocationVisibility:
    """评审 P2-6：共享索引在切换前不能让记录两边都查不到。"""

    @staticmethod
    def _vector() -> tuple[ShardRoutedVectorStore, InMemoryVectorStore]:
        inner = InMemoryVectorStore()
        routed = ShardRoutedVectorStore(
            resolver=resolver(), locator=DictLocator({"u1": "profile"}),
            mode="shared", inner=inner,
        )
        routed.insert(SCOPE, [vector_record("u1-0", "u1")])
        return routed, inner

    @staticmethod
    def _hits(routed, shard: str) -> list[str]:
        query = VectorQuery(vector=[1.0, 0.0], top_k=5, extensions={"shards": [shard]})
        return [hit.id for hit in routed.search(SCOPE, query)]

    def test_relocate_keeps_source_visible_and_prefilter_admits_target(self) -> None:
        routed, inner = self._vector()
        routed.relocate_unit(SCOPE, "u1", "profile", "working")

        stored = inner.get(SCOPE, ["u1-0"])[0].metadata
        assert stored[STORAGE_SHARD_FIELD] == "profile", "current tag unchanged before switch"
        assert stored[STORAGE_SHARD_NEXT_FIELD] == "working"
        assert self._hits(routed, "profile") == ["u1-0"]
        assert self._hits(routed, "working") == ["u1-0"], "prefilter must not drop the target"

    def test_cleanup_promotes_next_tag_and_rollback_drops_it(self) -> None:
        routed, inner = self._vector()
        routed.relocate_unit(SCOPE, "u1", "profile", "working")
        routed.cleanup_unit(SCOPE, "u1", "profile")
        stored = inner.get(SCOPE, ["u1-0"])[0].metadata
        assert stored[STORAGE_SHARD_FIELD] == "working"
        assert STORAGE_SHARD_NEXT_FIELD not in stored
        assert self._hits(routed, "profile") == []

        routed2, inner2 = self._vector()
        routed2.relocate_unit(SCOPE, "u1", "profile", "working")
        routed2.cleanup_unit(SCOPE, "u1", "working")  # 回滚
        stored2 = inner2.get(SCOPE, ["u1-0"])[0].metadata
        assert stored2[STORAGE_SHARD_FIELD] == "profile"
        assert STORAGE_SHARD_NEXT_FIELD not in stored2
        assert self._hits(routed2, "working") == []

    @staticmethod
    def test_shared_fulltext_relocation_keeps_both_visible_until_cleanup() -> None:
        inner = InMemoryFulltextStore(WhitespaceTokenizer())
        routed = ShardRoutedFulltextStore(
            resolver=resolver(), locator=DictLocator({"u1": "profile"}),
            mode="shared", inner=inner,
        )
        routed.insert(SCOPE, [Document(id="u1", text="alpha", metadata={"unit_id": "u1"})])
        routed.relocate_unit(SCOPE, "u1", "profile", "working")

        def hits(shard: str) -> list[str]:
            query = TextQuery(text="alpha", top_k=5, extensions={"shards": [shard]})
            return [hit.id for hit in routed.search(SCOPE, query)]

        assert hits("profile") == ["u1"] and hits("working") == ["u1"]
        routed.cleanup_unit(SCOPE, "u1", "profile")
        assert hits("profile") == [] and hits("working") == ["u1"]
