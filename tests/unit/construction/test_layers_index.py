"""L0/L1 分层索引单元测试。

验证设计 docs/features/layers-index-design.md：
- 带 layers 的 unit → L0/L1 表（独立 store 实例 = 分表）有记录，content 表不受污染；
- layers 空 unit → 不产生 L0/L1 记录；
- L0/L1 store 为 None → 跳过该层不报错（向后兼容）；
- update 先删后建（SUPERSEDE 场景旧分层不残留）；
- remove 幂等删 L0/L1 record。
"""

from common.type_def import ContentLayers, Scope
from construction.index_builder_impl.fulltext_index_builder import FulltextIndexBuilder
from construction.index_builder_impl.vector_index_builder import VectorIndexBuilder

from tests.unit.construction.fixtures import (
    MemoryFulltextStore,
    MemoryVectorStore,
    create_test_plugins,
    create_test_stores,
    create_test_unit,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _unit_with_layers(uid: str, content: str, l0: str = "", l1: str = ""):
    """构造带 layers 的 MemoryUnit。"""
    unit = create_test_unit(uid, content)
    unit.layers = ContentLayers(l0=l0, l1=l1)
    return unit


def _make_layered_vector_builder():
    """VectorIndexBuilder + content store + 独立 L0/L1 store（分表）。"""
    stores = create_test_stores()
    plugins = create_test_plugins()
    vector_l0 = MemoryVectorStore()
    vector_l1 = MemoryVectorStore()
    builder = VectorIndexBuilder(
        vector_store=stores["vector"],
        kv_store=stores["kv"],
        chunker=plugins["chunker"],
        embedder=plugins["embedder"],
        vector_l0=vector_l0,
        vector_l1=vector_l1,
    )
    return builder, stores, vector_l0, vector_l1, plugins


def _make_layered_fulltext_builder():
    """FulltextIndexBuilder + content store + 独立 L0/L1 store（分表）。"""
    stores = create_test_stores()
    fulltext_l0 = MemoryFulltextStore()
    fulltext_l1 = MemoryFulltextStore()
    builder = FulltextIndexBuilder(
        store=stores["fulltext"],
        fulltext_l0=fulltext_l0,
        fulltext_l1=fulltext_l1,
    )
    return builder, stores, fulltext_l0, fulltext_l1


# ---------------------------------------------------------------------------
# VectorIndexBuilder: 分层索引
# ---------------------------------------------------------------------------


def test_vector_layers_built_into_separate_stores():
    """带 layers 的 unit → L0/L1 record 落独立 store，content 表只含 chunk。"""
    builder, stores, vl0, vl1, _ = _make_layered_vector_builder()
    scope = Scope(org="test", user="alice")
    unit = _unit_with_layers("u1", "一段较长的内容用于构建索引", l0="概要", l1="要点概述")
    builder.build([unit])

    # L0/L1 表各有 1 条 record（id = {uid}-l0 / -l1）
    assert len(vl0.get(scope, ["u1-layer-l0"])) == 1
    assert vl0.get(scope, ["u1-layer-l0"])[0].metadata["content_layer"] == "l0"
    assert len(vl1.get(scope, ["u1-layer-l1"])) == 1
    assert vl1.get(scope, ["u1-layer-l1"])[0].metadata["content_layer"] == "l1"
    # content 表不含 -l0/-l1（分表不污染）
    assert stores["vector"].get(scope, ["u1-layer-l0", "u1-layer-l1"]) == []


def test_vector_layers_skipped_when_layers_empty():
    """layers 全空 → L0/L1 表无记录，不报错。"""
    builder, stores, vl0, vl1, _ = _make_layered_vector_builder()
    scope = Scope(org="test", user="alice")
    unit = create_test_unit("u1", "普通内容无分层")  # layers 默认空
    builder.build([unit])

    assert vl0.get(scope, ["u1-layer-l0"]) == []
    assert vl1.get(scope, ["u1-layer-l1"]) == []


def test_vector_layers_skipped_when_store_none():
    """L0/L1 store 为 None → 跳过该层不报错（向后兼容）。"""
    stores = create_test_stores()
    plugins = create_test_plugins()
    builder = VectorIndexBuilder(  # 不传 vector_l0/vector_l1 → None
        vector_store=stores["vector"],
        kv_store=stores["kv"],
        chunker=plugins["chunker"],
        embedder=plugins["embedder"],
    )
    scope = Scope(org="test", user="alice")
    unit = _unit_with_layers("u1", "内容", l0="概要", l1="要点")
    # 不抛异常即通过
    builder.build([unit])
    # content 表照常有 chunk record
    assert stores["vector"].get(scope, ["u1-layer-l0", "u1-layer-l1"]) == []


def test_vector_layers_partial_injection():
    """只注入 L0 没注入 L1 → 只建 L0，L1 跳过不报错。"""
    stores = create_test_stores()
    plugins = create_test_plugins()
    vector_l0 = MemoryVectorStore()  # 只配 L0
    builder = VectorIndexBuilder(
        vector_store=stores["vector"],
        kv_store=stores["kv"],
        chunker=plugins["chunker"],
        embedder=plugins["embedder"],
        vector_l0=vector_l0,
        # vector_l1 不传 → None
    )
    scope = Scope(org="test", user="alice")
    unit = _unit_with_layers("u1", "内容", l0="概要", l1="要点")
    builder.build([unit])

    assert len(vector_l0.get(scope, ["u1-layer-l0"])) == 1  # L0 建了
    # L1 没建（无 store），不报错


def test_vector_layers_update_delete_then_rebuild():
    """update：先删旧 L0/L1 record → 再按新 layers 重建（SUPERSEDE 场景）。"""
    builder, stores, vl0, vl1, _ = _make_layered_vector_builder()
    scope = Scope(org="test", user="alice")
    unit = _unit_with_layers("u1", "内容", l0="旧概要", l1="旧要点")
    builder.build([unit])
    assert len(vl0.get(scope, ["u1-layer-l0"])) == 1

    # 更新：layers 变化（L0 改、L1 清空）
    unit.layers = ContentLayers(l0="新概要", l1="")
    builder.update([unit])

    # L0 重建为新内容（旧 record 不残留，仍 1 条）
    l0_records = vl0.get(scope, ["u1-layer-l0"])
    assert len(l0_records) == 1
    # L1 因新 layers 空 → 不重建，旧 record 已删
    assert vl1.get(scope, ["u1-layer-l1"]) == []


def test_vector_layers_remove_idempotent():
    """remove：store 非空时按 id 幂等删 L0/L1 record。"""
    builder, stores, vl0, vl1, _ = _make_layered_vector_builder()
    scope = Scope(org="test", user="alice")
    unit = _unit_with_layers("u1", "内容", l0="概要", l1="要点")
    builder.build([unit])
    assert len(vl0.get(scope, ["u1-layer-l0"])) == 1

    builder.remove(["u1"])
    assert vl0.get(scope, ["u1-layer-l0"]) == []
    assert vl1.get(scope, ["u1-layer-l1"]) == []
    # 重复 remove 不报错（幂等）
    builder.remove(["u1"])


# ---------------------------------------------------------------------------
# FulltextIndexBuilder: 分层索引
# ---------------------------------------------------------------------------


def test_fulltext_layers_built_into_separate_stores():
    """带 layers 的 unit → L0/L1 文档落独立 store，content 表只含 unit.id。"""
    builder, stores, fl0, fl1 = _make_layered_fulltext_builder()
    scope = Scope(org="test", user="alice")
    unit = _unit_with_layers("u1", "一段内容", l0="概要", l1="要点概述")
    builder.build([unit])

    # L0/L1 表各有 1 条文档
    assert len(fl0.get(scope, ["u1:l0"])) == 1
    assert fl0.get(scope, ["u1:l0"])[0].text == "概要"
    assert fl0.get(scope, ["u1:l0"])[0].metadata["content_layer"] == "l0"
    assert len(fl1.get(scope, ["u1:l1"])) == 1
    assert fl1.get(scope, ["u1:l1"])[0].text == "要点概述"
    # content 表不含 -l0/-l1
    assert stores["fulltext"].get(scope, ["u1:l0", "u1:l1"]) == []


def test_fulltext_layers_skipped_when_store_none():
    """L0/L1 store 为 None → 跳过该层不报错。"""
    stores = create_test_stores()
    builder = FulltextIndexBuilder(store=stores["fulltext"])  # 不传 L0/L1
    scope = Scope(org="test", user="alice")
    unit = _unit_with_layers("u1", "内容", l0="概要", l1="要点")
    builder.build([unit])  # 不抛异常
    assert stores["fulltext"].get(scope, ["u1:l0", "u1:l1"]) == []


def test_fulltext_layers_update_delete_then_rebuild():
    """update：先删旧 L0/L1 → 再按新 layers 重建。"""
    builder, stores, fl0, fl1 = _make_layered_fulltext_builder()
    scope = Scope(org="test", user="alice")
    unit = _unit_with_layers("u1", "内容", l0="旧概要", l1="旧要点")
    builder.build([unit])

    # 更新：L0 改、L1 清空
    unit.layers = ContentLayers(l0="新概要", l1="")
    builder.update([unit])

    assert len(fl0.get(scope, ["u1:l0"])) == 1
    assert fl0.get(scope, ["u1:l0"])[0].text == "新概要"
    assert fl1.get(scope, ["u1:l1"]) == []  # L1 旧 record 已删，新 layers 空不重建


def test_fulltext_layers_remove_idempotent():
    """remove：幂等删 L0/L1 文档。"""
    builder, stores, fl0, fl1 = _make_layered_fulltext_builder()
    scope = Scope(org="test", user="alice")
    unit = _unit_with_layers("u1", "内容", l0="概要", l1="要点")
    builder.build([unit])

    builder.remove(["u1"])
    assert fl0.get(scope, ["u1:l0"]) == []
    assert fl1.get(scope, ["u1:l1"]) == []
    builder.remove(["u1"])  # 幂等不报错
