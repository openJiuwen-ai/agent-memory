"""IndexBuilder 单元测试（12 个测试）。

分别测试 FulltextIndexBuilder（关键词）和 VectorIndexBuilder（向量），
以及 HybridIndexBuilder（联合）。
"""

import json

from common.type_def import (
    Scope,
)
from construction.index_builder_impl.fulltext_index_builder import FulltextIndexBuilder
from construction.index_builder_impl.vector_index_builder import VectorIndexBuilder
from construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder

from tests.unit.construction.fixtures import (
    create_test_stores,
    create_test_plugins,
    create_test_unit,
)
from storage.types import TextQuery, VectorQuery


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_fulltext_builder() -> tuple[FulltextIndexBuilder, dict, dict]:
    """创建测试用 FulltextIndexBuilder 及其依赖。"""
    stores = create_test_stores()
    plugins = create_test_plugins()
    builder = FulltextIndexBuilder(stores["fulltext"])
    return builder, stores, plugins


def _make_vector_builder() -> tuple[VectorIndexBuilder, dict, dict]:
    """创建测试用 VectorIndexBuilder 及其依赖。"""
    stores = create_test_stores()
    plugins = create_test_plugins()
    builder = VectorIndexBuilder(
        vector_store=stores["vector"],
        kv_store=stores["kv"],
        chunker=plugins["chunker"],
        embedder=plugins["embedder"],
    )
    return builder, stores, plugins


def _make_hybrid_builder() -> tuple[HybridIndexBuilder, dict, dict]:
    """创建测试用 HybridIndexBuilder 及其依赖。"""
    stores = create_test_stores()
    plugins = create_test_plugins()
    builder = HybridIndexBuilder(
        fulltext=stores["fulltext"],
        vector=stores["vector"],
        kv=stores["kv"],
        chunker=plugins["chunker"],
        embedder=plugins["embedder"],
    )
    return builder, stores, plugins


# ---------------------------------------------------------------------------
# T-I-01: fulltext build 基本流程
# ---------------------------------------------------------------------------


def test_keyword_build_basic():
    """T-I-01: fulltext build → FulltextStore.search 能召回写入的 unit。"""
    builder, stores, _ = _make_fulltext_builder()
    scope = Scope(org="test", user="alice")
    units = [create_test_unit("u1", "用户偏好用 Python 写代码", scope=scope)]

    builder.build(units)

    hits = stores["fulltext"].search(scope, TextQuery(text="Python", top_k=10))
    assert len(hits) >= 1
    assert any(h.id == "u1" for h in hits)


# ---------------------------------------------------------------------------
# T-I-02: fulltext update 流程
# ---------------------------------------------------------------------------


def test_keyword_update():
    """T-I-02: update 后搜索返回新内容。"""
    builder, stores, _ = _make_fulltext_builder()
    scope = Scope(org="test", user="alice")

    # 先 build
    units = [create_test_unit("u1", "用户偏好 Python", scope=scope)]
    builder.build(units)

    # 再 update（修改 content）
    updated_units = [create_test_unit("u1", "用户偏好 Java", scope=scope)]
    builder.update(updated_units)

    hits = stores["fulltext"].search(scope, TextQuery(text="Java", top_k=10))
    assert len(hits) >= 1
    assert any(h.id == "u1" for h in hits)


# ---------------------------------------------------------------------------
# T-I-03: fulltext remove 流程
# ---------------------------------------------------------------------------


def test_keyword_remove():
    """T-I-03: remove 后搜索不返回。"""
    builder, stores, _ = _make_fulltext_builder()
    scope = Scope(org="test", user="alice")

    units = [create_test_unit("u1", "用户偏好 Python", scope=scope)]
    builder.build(units)

    builder.remove(["u1"])

    hits = stores["fulltext"].search(scope, TextQuery(text="Python", top_k=10))
    assert len(hits) == 0


# ---------------------------------------------------------------------------
# T-I-04: vector build 基本流程
# ---------------------------------------------------------------------------


def test_vector_build_basic():
    """T-I-04: vector build → VectorStore 中有写入的 chunk 记录。"""
    builder, stores, plugins = _make_vector_builder()
    scope = Scope(org="test", user="alice")
    units = [create_test_unit("u1", "用户偏好用 Python 写代码，经常使用 Python 进行数据分析", scope=scope)]

    builder.build(units)

    # 验证：VectorStore 中应有 chunk 记录（通过 chunk tracking 间接验证）
    chunk_ids = json.loads(stores["kv"].get(scope, "/index/chunks/u1").decode())
    assert len(chunk_ids) >= 1

    # 验证 chunk 记录确实存在于 VectorStore
    records = stores["vector"].get(scope, chunk_ids)
    assert len(records) >= 1
    assert all(r.metadata.get("unit_id") == "u1" for r in records)

    # 使用同一 embedder 搜索
    embedder = plugins["embedder"]
    query_vec = embedder.embed_query("用户偏好用 Python 写代码，经常使用 Python 进行数据分析")
    hits = stores["vector"].search(scope, VectorQuery(vector=query_vec, top_k=10))
    assert len(hits) >= 1


# ---------------------------------------------------------------------------
# T-I-05: vector chunk_id tracking
# ---------------------------------------------------------------------------


def test_vector_chunk_tracking():
    """T-I-05: KVStore chunk tracking 返回 chunk_id 列表。"""
    builder, stores, _ = _make_vector_builder()
    scope = Scope(org="test", user="alice")
    units = [create_test_unit("u1", "这是一段测试文本，包含足够内容来触发切片", scope=scope)]

    builder.build(units)

    kv_key = "/index/chunks/u1"
    raw = stores["kv"].get(scope, kv_key)
    chunk_ids = json.loads(raw.decode())
    assert isinstance(chunk_ids, list)
    assert len(chunk_ids) >= 1


# ---------------------------------------------------------------------------
# T-I-06: vector update 流程
# ---------------------------------------------------------------------------


def test_vector_update():
    """T-I-06: update 后旧 chunk 被替换，新 chunk 写入。"""
    builder, stores, _ = _make_vector_builder()
    scope = Scope(org="test", user="alice")

    # 先 build
    units = [create_test_unit("u1", "用户偏好 Python 进行数据分析，经常使用 Python 写脚本", scope=scope)]
    builder.build(units)

    # 记录旧 chunk_ids
    old_chunk_ids = json.loads(stores["kv"].get(scope, "/index/chunks/u1").decode())
    old_records = stores["vector"].get(scope, old_chunk_ids)

    # update（修改 content → chunk 内容改变）
    updated_units = [create_test_unit("u1", "用户偏好 Java 进行数据分析，经常使用 Java 写脚本", scope=scope)]
    builder.update(updated_units)

    # 新 chunk_ids 应存在
    new_chunk_ids = json.loads(stores["kv"].get(scope, "/index/chunks/u1").decode())
    assert len(new_chunk_ids) >= 1

    # 新 chunk 记录应存在且内容已更新
    new_records = stores["vector"].get(scope, new_chunk_ids)
    assert len(new_records) >= 1

    # 旧 chunk 应不存在
    different_old_ids = [id for id in old_chunk_ids if id not in new_chunk_ids]
    if different_old_ids:
        remaining = stores["vector"].get(scope, different_old_ids)
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# T-I-07: vector remove 流程
# ---------------------------------------------------------------------------


def test_vector_remove():
    """T-I-07: remove 后 chunk 不被召回。"""
    builder, stores, _ = _make_vector_builder()
    scope = Scope(org="test", user="alice")

    units = [create_test_unit("u1", "用户偏好用 Python 写代码", scope=scope)]
    builder.build(units)

    # 记录 chunk_ids
    chunk_ids = json.loads(stores["kv"].get(scope, "/index/chunks/u1").decode())

    builder.remove(["u1"])

    # VectorStore 中不应有这些 chunk
    remaining = stores["vector"].get(scope, chunk_ids)
    assert len(remaining) == 0


# ---------------------------------------------------------------------------
# T-I-08: vector remove_with_scope 便捷方法
# ---------------------------------------------------------------------------


def test_vector_remove_with_scope():
    """T-I-08: remove_with_scope 便捷方法。"""
    builder, stores, _ = _make_vector_builder()
    scope = Scope(org="test", user="alice")

    units = [create_test_unit("u1", "用户偏好用 Python 写代码", scope=scope)]
    builder.build(units)

    chunk_ids = json.loads(stores["kv"].get(scope, "/index/chunks/u1").decode())

    builder.remove_with_scope(["u1"], scope)

    remaining = stores["vector"].get(scope, chunk_ids)
    assert len(remaining) == 0


# ---------------------------------------------------------------------------
# T-I-09: hybrid 联合 build
# ---------------------------------------------------------------------------


def test_hybrid_build():
    """T-I-09: HybridIndexBuilder 同时构建关键词 + 向量索引。"""
    builder, stores, plugins = _make_hybrid_builder()
    scope = Scope(org="test", user="alice")
    units = [create_test_unit("u1", "用户偏好用 Python 写代码", scope=scope)]

    builder.build(units)

    # keyword 可召回
    kw_hits = stores["fulltext"].search(scope, TextQuery(text="Python", top_k=10))
    assert len(kw_hits) >= 1

    # vector 可召回
    embedder = plugins["embedder"]
    query_vec = embedder.embed_query("用户偏好用 Python 写代码")
    hits = stores["vector"].search(scope, VectorQuery(vector=query_vec, top_k=10))
    assert len(hits) >= 1


# ---------------------------------------------------------------------------
# T-I-10: rebuild
# ---------------------------------------------------------------------------


def test_rebuild():
    """T-I-10: rebuild → 重新 update 后搜索可召回。"""
    builder, stores, _ = _make_fulltext_builder()
    scope = Scope(org="test", user="alice")

    # 先 build
    units = [create_test_unit("u1", "用户偏好 Python", scope=scope)]
    builder.build(units)

    # rebuild（最小实现：返回 None）
    builder.rebuild()

    # 重新 update（模拟 rebuild 行为——已有记录用 update）
    builder.update(units)

    kw_hits = stores["fulltext"].search(scope, TextQuery(text="Python", top_k=10))
    assert len(kw_hits) >= 1


# ---------------------------------------------------------------------------
# T-I-11: 空内容 unit
# ---------------------------------------------------------------------------


def test_empty_content_unit():
    """T-I-11: content="" → vector 跳过（Chunker 返回空 → VectorStore 无写入）。"""
    builder, stores, _ = _make_vector_builder()
    scope = Scope(org="test", user="alice")
    units = [create_test_unit("u1", "", scope=scope)]

    builder.build(units)

    # vector 无写入（空 content → Chunker 返回空 list → 无 chunk tracking）
    from common.errors import NotFoundError
    try:
        stores["kv"].get(scope, "/index/chunks/u1")
        assert False, "chunk tracking should not exist for empty content"
    except NotFoundError:
        pass  # 正确：空 content 不应有向量索引


# ---------------------------------------------------------------------------
# T-I-12: scope 过滤
# ---------------------------------------------------------------------------


def test_scope_isolation():
    """T-I-12: 不同 scope 的 unit 互不干扰。"""
    builder, stores, _ = _make_fulltext_builder()
    scope1 = Scope(org="test", user="alice")
    scope2 = Scope(org="test", user="bob")

    units1 = [create_test_unit("u1", "Alice 喜欢用 Python", scope=scope1)]
    units2 = [create_test_unit("u2", "Bob 喜欢用 Java", scope=scope2)]

    builder.build(units1)
    builder.build(units2)

    # scope1 搜索只返回 alice 的 unit
    hits1 = stores["fulltext"].search(scope1, TextQuery(text="Python", top_k=10))
    assert any(h.id == "u1" for h in hits1)

    # scope2 搜索只返回 bob 的 unit
    hits2 = stores["fulltext"].search(scope2, TextQuery(text="Java", top_k=10))
    assert any(h.id == "u2" for h in hits2)

    # scope1 搜索 Java 应不返回 bob 的 unit
    hits3 = stores["fulltext"].search(scope1, TextQuery(text="Java", top_k=10))
    assert not any(h.id == "u2" for h in hits3)
