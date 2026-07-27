"""IndexBuilder 单元测试。

分别测试 FulltextIndexBuilder（关键词）和 VectorIndexBuilder（向量），
以及 HybridIndexBuilder（联合）。
"""

import json
from datetime import datetime, timezone

import pytest

from common.bootstrap import register_plugins
from common.factory.factory import Factory
from common.type_def import (
    T_INVALID_OPEN,
    Scope,
)
from config.context import AssemblyContext
from construction.bootstrap import register_constructors
from construction.index_builder import IndexBuilderProducer
from construction.index_builder_impl.fulltext_index_builder import FulltextIndexBuilder
from construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from construction.index_builder_impl.vector_index_builder import VectorIndexBuilder
from storage.bootstrap import register_backends
from storage.types import TextQuery, VectorQuery
from tests.unit.construction.fixtures import (
    create_test_plugins,
    create_test_stores,
    create_test_unit,
)

pytestmark = pytest.mark.unit


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

    builder.remove(units)

    hits = stores["fulltext"].search(scope, TextQuery(text="Python", top_k=10))
    assert len(hits) == 0


def test_keyword_remove_is_bound_to_each_units_scope():
    builder, stores, _ = _make_fulltext_builder()
    scope_a = Scope(org="test", space="space-a", user="alice")
    scope_b = Scope(org="test", space="space-b", user="alice")
    unit_a = create_test_unit("shared-id", "space A Python", scope=scope_a)
    unit_b = create_test_unit("shared-id", "space B Python", scope=scope_b)
    builder.build([unit_a, unit_b])

    builder.remove([unit_b])

    assert stores["fulltext"].get(scope_a, [unit_a.id])
    assert stores["fulltext"].get(scope_b, [unit_b.id]) == []


# ---------------------------------------------------------------------------
# T-I-04: vector build 基本流程
# ---------------------------------------------------------------------------


def test_vector_build_basic():
    """T-I-04: vector build → VectorStore 中有写入的 chunk 记录。"""
    builder, stores, plugins = _make_vector_builder()
    scope = Scope(org="test", user="alice")
    units = [
        create_test_unit(
            "u1",
            "用户偏好用 Python 写代码，经常使用 Python 进行数据分析",
            scope=scope,
        )
    ]

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


def test_index_builders_project_user_metadata_for_filtering():
    scope = Scope(org="test", user="alice")
    unit = create_test_unit("u1", "metadata projection", scope=scope)
    unit.metadata.update(
        {
            "memory_type": "coding",
            "project": "alpha",
            # 非字符串标量原样带入——后端据此建 double/boolean mapping 才能原生下推
            "priority": 8,
            "score": 9.5,
            "archived": False,
            "unit_id": "must-not-override-system-id",
            "lifecycle": "must-not-override-system-lifecycle",
        }
    )
    unit.tags = ["work"]

    fulltext_builder, fulltext_stores, _ = _make_fulltext_builder()
    fulltext_builder.build([unit])
    doc = fulltext_stores["fulltext"].get(scope, ["u1"])[0]

    assert doc.metadata["memory_type"] == "coding"
    assert doc.metadata["project"] == "alpha"
    assert doc.metadata["tags"] == ["work"]
    assert doc.metadata["unit_id"] == "u1"
    assert doc.metadata["lifecycle"] == "active"
    # 类型不得在投影处被改写：字符串化会让 range 退化成字典序
    assert doc.metadata["priority"] == 8 and not isinstance(doc.metadata["priority"], str)
    assert doc.metadata["score"] == 9.5
    assert doc.metadata["archived"] is False

    vector_builder, vector_stores, _ = _make_vector_builder()
    vector_builder.build([unit])
    chunk_ids = json.loads(vector_stores["kv"].get(scope, "/index/chunks/u1").decode())
    records = vector_stores["vector"].get(scope, chunk_ids)

    assert records
    assert all(record.metadata["memory_type"] == "coding" for record in records)
    assert all(record.metadata["project"] == "alpha" for record in records)
    assert all(record.metadata["tags"] == ["work"] for record in records)
    assert all(record.metadata["unit_id"] == "u1" for record in records)
    assert all(record.metadata["lifecycle"] == "active" for record in records)
    assert all(record.metadata["priority"] == 8 for record in records)
    assert all(record.metadata["score"] == 9.5 for record in records)
    assert all(record.metadata["archived"] is False for record in records)


def test_index_builders_write_sentinel_for_open_ended_t_invalid():
    """t_invalid 为空（永久有效）时索引落哨兵，非空时落真实时间戳。

    字段缺失会被 `t_invalid > as_of` 的下推按缺失字段排他，滤掉回溯查询最该命中的
    活跃记忆。哨兵只在索引层，真源 temporal.t_invalid 仍是 None。
    """
    scope = Scope(org="test", user="alice")
    open_unit = create_test_unit("u_open", "open ended", scope=scope)
    closed_unit = create_test_unit("u_closed", "already invalid", scope=scope)
    invalid_at = datetime(2026, 6, 16, tzinfo=timezone.utc)
    closed_unit.temporal.t_invalid = invalid_at

    fulltext_builder, fulltext_stores, _ = _make_fulltext_builder()
    fulltext_builder.build([open_unit, closed_unit])
    docs = {d.id: d for d in fulltext_stores["fulltext"].get(scope, ["u_open", "u_closed"])}

    assert docs["u_open"].metadata["t_invalid"] == T_INVALID_OPEN
    assert docs["u_closed"].metadata["t_invalid"] == int(invalid_at.timestamp() * 1000)
    # 真源不受影响：哨兵是索引投影的约定，valid_at 仍按 None 判"永久有效"
    assert open_unit.temporal.t_invalid is None

    vector_builder, vector_stores, _ = _make_vector_builder()
    vector_builder.build([open_unit])
    chunk_ids = json.loads(vector_stores["kv"].get(scope, "/index/chunks/u_open").decode())
    records = vector_stores["vector"].get(scope, chunk_ids)

    assert records
    assert all(record.metadata["t_invalid"] == T_INVALID_OPEN for record in records)


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
    units = [
        create_test_unit(
            "u1",
            "用户偏好 Python 进行数据分析，经常使用 Python 写脚本",
            scope=scope,
        )
    ]
    builder.build(units)

    # 记录旧 chunk_ids
    old_chunk_ids = json.loads(stores["kv"].get(scope, "/index/chunks/u1").decode())

    # update（修改 content → chunk 内容改变）
    updated_units = [
        create_test_unit(
            "u1",
            "用户偏好 Java 进行数据分析，经常使用 Java 写脚本",
            scope=scope,
        )
    ]
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

    builder.remove(units)

    # VectorStore 中不应有这些 chunk
    remaining = stores["vector"].get(scope, chunk_ids)
    assert len(remaining) == 0


def test_vector_build_and_remove_are_bound_to_each_units_scope():
    builder, stores, _ = _make_vector_builder()
    scope_a = Scope(org="test", space="space-a", user="alice")
    scope_b = Scope(org="test", space="space-b", user="alice")
    unit_a = create_test_unit("shared-id", "space A Python vector content", scope=scope_a)
    unit_b = create_test_unit("shared-id", "space B Python vector content", scope=scope_b)

    builder.build([unit_a, unit_b])

    chunk_ids_a = json.loads(stores["kv"].get(scope_a, "/index/chunks/shared-id").decode())
    chunk_ids_b = json.loads(stores["kv"].get(scope_b, "/index/chunks/shared-id").decode())
    assert stores["vector"].get(scope_a, chunk_ids_a)
    assert stores["vector"].get(scope_b, chunk_ids_b)

    builder.remove([unit_b])

    assert stores["vector"].get(scope_a, chunk_ids_a)
    assert stores["vector"].get(scope_b, chunk_ids_b) == []


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


# ---------------------------------------------------------------------------
# T-I-13/T-I-14: 工厂级分层索引注入（回归防护）
#
# fulltext/vector 工厂的 _build 必须按 layers_index_enabled + layers_l0/l1 具名
# 实例注入分层 store——否则用户未用 hybrid 模式时，分层索引静默失效（即便
# defaults 已配 layers_l0/l1 分表）。与 HybridIndexBuilder._build 行为对齐。
# ---------------------------------------------------------------------------


def _bootstrap_factories():
    """触发 store + 构造器注册，并返回重置句柄。"""
    register_plugins()
    register_backends()
    register_constructors()
    Factory.reset_all()
    return Factory.reset_all


def _layered_ctx():
    """layers_index_enabled=True 且 store 命名空间下声明 layers_l0/l1 具名实例。"""
    return AssemblyContext.from_dict(
        {
            # store 命名空间（_opt_dep 按 VectorProducer/FulltextProducer.TOP_NAME 查这里）
            "vector_store": {
                "shared": {"target": "memory"},
                "layers_l0": {"target": "memory"},
                "layers_l1": {"target": "memory"},
            },
            "fulltext_store": {
                "shared": {"target": "memory"},
                "layers_l0": {"target": "memory"},
                "layers_l1": {"target": "memory"},
            },
            "kv_store": {"shared": {"target": "memory"}},
            # 构造器命名空间（IndexBuilderProducer.TOP_NAME == "constructor"）
            "constructor": {
                "fb": {
                    "target": "fulltext",
                    "params": {
                        "fulltext_store": "shared",
                        "layers_index_enabled": True,
                    },
                },
                "vb": {
                    "target": "vector",
                    "params": {
                        "vector_store": "shared",
                        "kv_store": "shared",
                        "layers_index_enabled": True,
                    },
                },
            },
        }
    )


def test_fulltext_factory_injects_layer_stores():
    """fulltext 工厂按 layers_l0/l1 具名实例注入分层 store（非 None）。"""
    teardown = _bootstrap_factories()
    try:
        ctx = _layered_ctx()
        builder = IndexBuilderProducer.build_named("fb", ctx)
        assert builder.fulltext_l0 is not None, "fulltext 工厂未注入 layers_l0 store"
        assert builder.fulltext_l1 is not None, "fulltext 工厂未注入 layers_l1 store"
    finally:
        teardown()


def test_vector_factory_injects_layer_stores():
    """vector 工厂按 layers_l0/l1 具名实例注入分层 store（非 None）。"""
    teardown = _bootstrap_factories()
    try:
        ctx = _layered_ctx()
        builder = IndexBuilderProducer.build_named("vb", ctx)
        assert builder.vector_l0 is not None, "vector 工厂未注入 layers_l0 store"
        assert builder.vector_l1 is not None, "vector 工厂未注入 layers_l1 store"
    finally:
        teardown()


def test_fulltext_factory_skips_layers_when_disabled():
    """layers_index_enabled=False → 分层 store 为 None（向后兼容 + 配置降级）。"""
    teardown = _bootstrap_factories()
    try:
        ctx = _layered_ctx()
        # 关掉分层开关：即便 namespace 声明了 layers_l0/l1，也不注入
        ctx.namespaces["constructor"]["fb"].params["layers_index_enabled"] = False
        builder = IndexBuilderProducer.build_named("fb", ctx)
        assert builder.fulltext_l0 is None
        assert builder.fulltext_l1 is None
    finally:
        teardown()


# ---------------------------------------------------------------------------
# T-I-15: content 切不出 chunk 时仍建 L0/L1 分层索引（回归防护）
#
# build() 的 ``if not all_records: return`` 提前返回曾位于 _build_layers 调用之前，
# 导致所有 unit content 为空（chunker 返空）→ all_records 空 → _build_layers 永不执行，
# 即使 unit.layers.l0/l1 非空也不建分层索引。修复后 _build_layers 移到提前返回之前。
# ---------------------------------------------------------------------------


def test_build_layers_runs_even_when_no_content_chunks():
    """content 全空（无 chunk）但 layers.l0/l1 非空 → 分层 store 仍应有 L0/L1 record。"""
    from common.type_def import ContentLayers, MemoryUnit, Modality, Segment
    from tests.unit.construction.fixtures import MemoryVectorStore

    scope = Scope(org="test", user="alice")
    # content 空（Segment 内容空 → chunker 返空 → 无 chunk record）但 layers 非空
    unit = MemoryUnit(
        id="u1",
        scope=scope,
        segments=[Segment(content="", source=Modality.TEXT)],
        layers=ContentLayers(l0="用户偏好 Python 的概要", l1="用户偏好 Python 的要点片段"),
    )

    vector_l0 = MemoryVectorStore()
    vector_l1 = MemoryVectorStore()
    content_store = MemoryVectorStore()
    stores = create_test_stores()
    plugins = create_test_plugins()
    builder = VectorIndexBuilder(
        vector_store=content_store,
        kv_store=stores["kv"],
        chunker=plugins["chunker"],
        embedder=plugins["embedder"],
        vector_l0=vector_l0,
        vector_l1=vector_l1,
    )

    builder.build([unit])

    # content 表无 chunk record（空 content → chunker 返空）
    assert content_store.get(scope, []) == []

    # L0/L1 分层 store 应有对应 record（_build_layers 在提前返回之前执行）
    l0_records = vector_l0.get(scope, ["u1-layer-l0"])
    assert len(l0_records) == 1, "L0 分层索引未构建：content 无 chunk 时 _build_layers 应仍执行"
    assert l0_records[0].metadata.get("content_layer") == "l0"

    l1_records = vector_l1.get(scope, ["u1-layer-l1"])
    assert len(l1_records) == 1, "L1 分层索引未构建：content 无 chunk 时 _build_layers 应仍执行"
    assert l1_records[0].metadata.get("content_layer") == "l1"
