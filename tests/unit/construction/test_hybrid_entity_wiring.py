"""HybridIndexBuilder 组合 EntityIndexBuilder 的单元测试。

对应提交 093cd82c：``HybridIndexBuilder`` 新增 ``entity_linker`` 注入，
build/update/remove 委托给内部 entity 子 builder。``entity_linker=None``
时跳过 entity 子 builder（降级链路：endpoint 未配）。

验证方式：构造 HybridIndexBuilder 时注入真实 EntityLinkService（依赖内存版
entity store），观察 build/update/remove 后 entity store 的副作用，确认
委托生效。不访问受保护成员，全走 HybridIndexBuilder 公开 API（build/update/remove）。

2026-08-12 改造后 ``EntityLinkService`` 构造函数删除 ``extractor`` 参数
（砍 spaCy 兜底），只消费 ``unit.entities`` 明文。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.chunker.chunker_impl.recursive_chunker import RecursiveChunker
from jiuwen_memory.common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Segment
from jiuwen_memory.common.type_def.scope import Scope, space_id_from_scope
from jiuwen_memory.construction.index_builder_impl.entity_index_builder import (
    EntityIndexAdmissionPolicy,
    EntityLinkService,
)
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage
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
    storage = CompositeStorage(
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
    storage = CompositeStorage(
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
    storage = CompositeStorage(
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
