"""HybridIndexBuilder 组合 EntityIndexBuilder 的单元测试。

对应提交 093cd82c：``HybridIndexBuilder`` 新增 ``entity_linker`` 注入，
build/update/remove 委托给内部 entity 子 builder。``entity_linker=None``
时跳过 entity 子 builder（降级链路：endpoint 未配）。

验证方式：构造 HybridIndexBuilder 时注入真实 EntityLinkService（依赖内存版
entity store），观察 build/update/remove 后 entity store 的副作用，确认
委托生效。不访问受保护成员，全走 HybridIndexBuilder 公开 API（build/update/remove）。

2026-08-12 改造后 ``EntityLinkService`` 构造函数删除 ``extractor`` 参数
（砍 spaCy 兜底），只消费 ``unit.entities`` 明文。

F07-D 起本文件另覆盖**装配侧**：两个消费方（写入侧 hybrid、召回侧 keyword）都经
``manager.entity(name)`` 取 ENTITY 端口，不再各自 ``EntityStoreProducer.dep``；
端口未装配时降级关闭。核心用例是"读写共享同一端口"——F07-D 之前这靠两侧 params
各自引用同一具名实例的配置纪律维持，现在由 manager 保证。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.chunker.chunker_impl.recursive_chunker import RecursiveChunker
from jiuwen_memory.common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Segment
from jiuwen_memory.common.type_def.scope import Scope, space_id_from_scope
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.construction.index_builder_impl.entity_index_builder import (
    EntityIndexAdmissionPolicy,
    EntityLinkService,
)
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.retrieval.recaller import RecallerProducer
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.store_manager import StoreManagerProducer
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

# 复用 linker 测试的内存版 entity store 桩
from tests.unit.construction.test_entity_linker import InMemoryEntityStore

pytestmark = pytest.mark.unit


def _make_unit(
    uid: str,
    content: str,
    *,
    entities: list[str] | None = None,
    tier: MemoryTier = MemoryTier.SEMANTIC,
) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=Scope(org="o1", user="u1", agent="a1", session="s1"),
        tier=tier,
        segments=[Segment(content=content)],
        entities=list(entities or []),
    )


def _make_hybrid(
    entity_store: InMemoryEntityStore,
) -> tuple[HybridIndexBuilder, EntityLinkService]:
    """构造注入了 entity 子 builder 的 HybridIndexBuilder。"""
    embedder = HashingEmbedder(WhitespaceTokenizer())
    storage = CompositeStoreManager(
        kv=InMemoryKVStore(),
        vector=InMemoryVectorStore(),
        fulltext=InMemoryFulltextStore(WhitespaceTokenizer()),
    )
    linker = EntityLinkService(
        entity_store=entity_store,
        admission_policy=EntityIndexAdmissionPolicy(),
    )
    builder = HybridIndexBuilder(
        storage,
        chunker=RecursiveChunker(chunk_size_chars=50, overlap_chars=10, min_chunk_chars=5),
        embedder=embedder,
        entity_linker=linker,
    )
    return builder, linker


def _records(store: InMemoryEntityStore, scope: Scope) -> list:
    return store.records(space_id_from_scope(scope))


# ---------------------------------------------------------------------------
# build 委托
# ---------------------------------------------------------------------------


def test_build_delegates_to_entity_builder() -> None:
    store = InMemoryEntityStore()
    builder, _ = _make_hybrid(store)
    unit = _make_unit("u1", "x", entities=["Alice"])
    scope = unit.scope

    builder.build([unit])

    # entity 子 builder 委托 link_memories → store 里有 Alice 记录关联 u1
    recs = _records(store, scope)
    assert len(recs) == 1
    assert "u1" in recs[0].linked_memory_ids


def test_build_skips_entity_when_no_linker() -> None:
    # entity_linker=None → HybridIndexBuilder 不构造 entity 子 builder，
    # build 不触碰 entity store（降级链路，向后兼容）
    embedder = HashingEmbedder(WhitespaceTokenizer())
    storage = CompositeStoreManager(
        kv=InMemoryKVStore(),
        vector=InMemoryVectorStore(),
        fulltext=InMemoryFulltextStore(WhitespaceTokenizer()),
    )
    builder = HybridIndexBuilder(
        storage,
        chunker=RecursiveChunker(chunk_size_chars=50, overlap_chars=10, min_chunk_chars=5),
        embedder=embedder,
        entity_linker=None,
    )
    unit = _make_unit("u1", "x", entities=["Alice"])
    # 不应抛异常（fulltext/vector 子 builder 正常跑，entity 跳过）
    builder.build([unit])


# ---------------------------------------------------------------------------
# update / remove 委托
# ---------------------------------------------------------------------------


def test_remove_delegates_to_entity_unlink() -> None:
    store = InMemoryEntityStore()
    builder, _ = _make_hybrid(store)
    unit = _make_unit("u1", "x", entities=["Alice"])
    scope = unit.scope

    builder.build([unit])
    assert _records(store, scope)  # 有记录

    builder.remove([unit])

    # entity 子 builder 委托 unlink_memory → 实体只关联 u1，删后空 → DELETE
    assert _records(store, scope) == []


def test_update_delegates_unlink_then_link() -> None:
    store = InMemoryEntityStore()
    builder, _ = _make_hybrid(store)
    u1 = _make_unit("u1", "x", entities=["Alice"])
    scope = u1.scope

    builder.build([u1])
    # update：先 unlink 旧实体链接，再 link 新内容实体
    u1_new = _make_unit("u1", "x", entities=["Bob"])
    builder.update([u1_new])

    recs = _records(store, scope)
    # 旧 Alice 被 unlink（只关联 u1 → 空 → DELETE）；新 Bob INSERT
    names = {r.entity_text for r in recs}
    assert "Bob" in names
    assert "Alice" not in names


# ---------------------------------------------------------------------------
# 容错：entity 链路失败不中断主链路
# ---------------------------------------------------------------------------


def test_build_tolerates_entity_linker_failure() -> None:
    # EntityIndexBuilder.build 内部 try/except：link_memories 抛异常只 log warning，
    # 不中断主链路（fulltext/vector 仍写成功）。
    class _BoomLinker(EntityLinkService):
        @staticmethod
        def link_memories(units):
            raise RuntimeError("entity store down")

    embedder = HashingEmbedder(WhitespaceTokenizer())
    storage = CompositeStoreManager(
        kv=InMemoryKVStore(),
        vector=InMemoryVectorStore(),
        fulltext=InMemoryFulltextStore(WhitespaceTokenizer()),
    )
    boom_linker = _BoomLinker(
        entity_store=InMemoryEntityStore(),
        admission_policy=EntityIndexAdmissionPolicy(),
    )
    builder = HybridIndexBuilder(
        storage,
        chunker=RecursiveChunker(chunk_size_chars=50, overlap_chars=10, min_chunk_chars=5),
        embedder=embedder,
        entity_linker=boom_linker,
    )
    unit = _make_unit("u1", "alice likes coffee", entities=["Alice"])
    # 不应抛异常（entity 失败被 EntityIndexBuilder.build 兜住，主链路照常完成）
    builder.build([unit])


# ---------------------------------------------------------------------------
# 装配侧（F07-D）：两个消费方经 manager 的 ENTITY 端口取实例
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_factory():
    """具名实例缓存跨测试隔离：装配用例经 StoreManagerProducer.put/resolve。"""
    Factory.reset_all()
    yield
    Factory.reset_all()


class _TracingEntityStore(InMemoryEntityStore):
    """在内存实现之上记录被调方法名，用于区分写入侧与召回侧的调用来源。"""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def execute_operations(self, space_id, operations):
        self.seen.append("execute_operations")
        return super().execute_operations(space_id, operations)

    def find_by_entity_text_hash(self, space_id, entity_text_hashes, *, filters, limit=500):
        self.seen.append("find_by_entity_text_hash")
        return super().find_by_entity_text_hash(
            space_id, entity_text_hashes, filters=filters, limit=limit
        )


def _wire(entity_store) -> AssemblyContext:
    """预置一个含 ENTITY 端口的 manager，返回消费方装配用的 ctx。"""
    manager = CompositeStoreManager(
        kv=InMemoryKVStore(),
        vector=InMemoryVectorStore(),
        fulltext=InMemoryFulltextStore(WhitespaceTokenizer()),
        entity=entity_store,
    )
    StoreManagerProducer.put("main", manager)
    return AssemblyContext.from_dict(
        {
            "globals": {
                "store_manager": "main",
                "entity_enabled": True,
                "vector_enabled": False,
                "layers_index_enabled": False,
            }
        }
    )


def test_hybrid_builder_takes_entity_from_manager_port(_clean_factory) -> None:
    store = _TracingEntityStore()
    builder = IndexBuilderProducer.build("hybrid", {}, _wire(store))

    unit = _make_unit("u1", "alice likes coffee", entities=["Alice"])
    builder.build([unit])

    # 端口取到了实例，entity 子 builder 生效
    assert "execute_operations" in store.seen
    assert _records(store, unit.scope)


def test_hybrid_builder_degrades_when_manager_has_no_entity_port(_clean_factory) -> None:
    """entity_enabled=true 但 manager 无 ENTITY 端口 → 降级关闭，主链路照常。"""
    manager = CompositeStoreManager(
        kv=InMemoryKVStore(),
        vector=InMemoryVectorStore(),
        fulltext=InMemoryFulltextStore(WhitespaceTokenizer()),
    )
    StoreManagerProducer.put("main", manager)
    ctx = AssemblyContext.from_dict(
        {
            "globals": {
                "store_manager": "main",
                "entity_enabled": True,
                "vector_enabled": False,
                "layers_index_enabled": False,
            }
        }
    )

    builder = IndexBuilderProducer.build("hybrid", {}, ctx)
    # 不抛异常：fulltext/vector 子 builder 正常跑，entity 跳过
    builder.build([_make_unit("u1", "alice likes coffee", entities=["Alice"])])


def test_entity_disabled_skips_port_lookup(_clean_factory) -> None:
    """entity_enabled=false 时短路，不取端口（即便端口已装配）。"""
    store = _TracingEntityStore()
    manager = CompositeStoreManager(
        kv=InMemoryKVStore(),
        vector=InMemoryVectorStore(),
        fulltext=InMemoryFulltextStore(WhitespaceTokenizer()),
        entity=store,
    )
    StoreManagerProducer.put("main", manager)
    ctx = AssemblyContext.from_dict(
        {
            "globals": {
                "store_manager": "main",
                "entity_enabled": False,
                "vector_enabled": False,
                "layers_index_enabled": False,
            }
        }
    )

    builder = IndexBuilderProducer.build("hybrid", {}, ctx)
    builder.build([_make_unit("u1", "alice likes coffee", entities=["Alice"])])

    assert store.seen == []


def test_builder_and_recaller_share_the_same_entity_port(_clean_factory) -> None:
    """F07-D 核心收益：读写两侧取的是同一 manager 的同一 ENTITY 端口。

    写入侧留下 execute_operations 记录、召回侧留下 find_by_entity_text_hash 记录，
    两类记录出现在**同一个** entity store 实例上，即证明共享（改造前两侧各自
    ``EntityStoreProducer.dep``，缺省时会各建一个匿名实例）。
    """
    store = _TracingEntityStore()
    ctx = _wire(store)

    builder = IndexBuilderProducer.build("hybrid", {}, ctx)
    recaller = RecallerProducer.build("keyword", {}, ctx)

    unit = _make_unit("u1", "alice likes coffee", entities=["Alice"])
    builder.build([unit])
    # 写入侧打到本实例（link_memories 先 hash 反查再 bulk 写）
    assert "execute_operations" in store.seen
    store.seen.clear()  # 清空后再验召回侧，才能区分调用来源

    from jiuwen_memory.common.type_def import ParsedQuery

    recaller.recall(unit.scope, ParsedQuery(raw="coffee"), top_k=10)

    # 召回侧的反查落在**同一个**实例上 → 读写共享同一 ENTITY 端口
    assert "find_by_entity_text_hash" in store.seen
