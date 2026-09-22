# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""树字段在各索引入口中一致投影，退树清旧字段且与向量本体往返兼容。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.type_def import (
    HIERARCHY_INDEX_KEYS,
    ChunkVector,
    ContentLayers,
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    MemoryUnit,
    Segment,
)
from jiuwen_memory.construction.index_builder_impl.fulltext_index_builder import (
    FulltextIndexBuilder,
)
from jiuwen_memory.construction.index_builder_impl.unified_index_builder import UnifiedIndexBuilder
from jiuwen_memory.construction.index_builder_impl.vector_index_builder import VectorIndexBuilder
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from tests.conftest import make_storage
from tests.unit.construction.fixtures import (
    MemoryFulltextStore,
    MemoryVectorStore,
    create_test_plugins,
    create_test_stores,
    create_test_unit,
)

pytestmark = pytest.mark.unit

_TREE_METADATA = {
    "hierarchy_kind": "time",
    "hierarchy_role": "snapshot",
    "hierarchy_status": "active",
    "parent_id": "tree-parent",
    "span_start": 1787011200123,
    "span_end": 1787014800123,
}


def _tree_unit() -> MemoryUnit:
    unit = create_test_unit("tree-u", "remember project architecture")
    unit.layers = ContentLayers(l0="project", l1="project architecture")
    start = datetime(2026, 8, 18, 8, 0, 0, 123000, tzinfo=timezone(timedelta(hours=8)))
    unit.hierarchy = HierarchyRef(
        kind=HierarchyKind.TIME,
        role=HierarchyRole.SNAPSHOT,
        parent_id="tree-parent",
        span_start=start,
        span_end=start + timedelta(hours=1),
    )
    return unit


@pytest.mark.parametrize("kind", ["fulltext", "vector"])
def test_all_content_layers_receive_and_clear_hierarchy_projection(kind: str) -> None:
    unit = _tree_unit()
    unit.user_metadata["hierarchy_role"] = "business-role"
    stores = create_test_stores()
    plugins = create_test_plugins()
    store_type = MemoryFulltextStore if kind == "fulltext" else MemoryVectorStore
    ports = {"default": stores[kind], "layers_l0": store_type(), "layers_l1": store_type()}
    manager = CompositeStoreManager(kv=stores["kv"], **{kind: ports})
    if kind == "fulltext":
        builder = FulltextIndexBuilder(manager)
    else:
        builder = VectorIndexBuilder(manager, plugins["chunker"], plugins["embedder"])
    builder.build([unit])

    if kind == "fulltext":
        record_ids = {
            "default": [unit.id],
            "layers_l0": [f"{unit.id}:l0"],
            "layers_l1": [f"{unit.id}:l1"],
        }
    else:
        chunk_ids = json.loads(stores["kv"].get(unit.scope, f"/index/chunks/{unit.id}"))
        record_ids = {
            "default": chunk_ids,
            "layers_l0": [f"{unit.id}-layer-l0"],
            "layers_l1": [f"{unit.id}-layer-l1"],
        }
    for name, port in ports.items():
        ids = record_ids.get(name)
        assert ids is not None, f"{kind}/{name} 缺少索引记录 ID"
        records = port.get(unit.scope, ids)
        assert records, f"{kind}/{name} 必须实际写入索引记录"
        for record in records:
            assert {key: record.metadata.get(key) for key in _TREE_METADATA} == _TREE_METADATA
            assert record.metadata["user_metadata.hierarchy_role"] == "business-role"

    unit.hierarchy = HierarchyRef()
    builder.update([unit])
    for name, port in ports.items():
        ids = record_ids.get(name)
        assert ids is not None, f"{kind}/{name} 缺少索引记录 ID"
        records = port.get(unit.scope, ids)
        assert records, f"{kind}/{name} 退树后保留内容索引"
        assert all(
            not set(_TREE_METADATA).intersection(indexed_record.metadata)
            for indexed_record in records
        )
        assert all(
            indexed_record.metadata["user_metadata.hierarchy_role"] == "business-role"
            for indexed_record in records
        )


def test_unified_projects_hierarchy_and_clears_stale_fields_on_update() -> None:
    stores = create_test_stores()
    domain = make_storage(kv=stores["kv"]).domain_store()
    builder = UnifiedIndexBuilder(domain, vector_enabled=False)
    unit = _tree_unit()
    unit.system_metadata.update(dict.fromkeys(_TREE_METADATA, "stale-value"))
    unit.system_metadata["custom_flag"] = "preserved"
    unit.user_metadata["hierarchy_role"] = "business-role"

    builder.build([unit])

    persisted = domain.get(unit.scope, [unit.id])[0]
    assert {key: persisted.system_metadata[key] for key in _TREE_METADATA} == _TREE_METADATA
    unit.hierarchy = HierarchyRef(kind=HierarchyKind.TOPIC, role=HierarchyRole.NODE)
    builder.update([unit])
    persisted = domain.get(unit.scope, [unit.id])[0]
    assert "span_start" not in persisted.system_metadata
    assert "span_end" not in persisted.system_metadata
    assert persisted.system_metadata["hierarchy_kind"] == "topic"
    assert persisted.system_metadata["hierarchy_role"] == "node"
    assert persisted.system_metadata["parent_id"] == ""

    unit.hierarchy = HierarchyRef()
    builder.update([unit])
    persisted = domain.get(unit.scope, [unit.id])[0]
    assert not set(_TREE_METADATA).intersection(persisted.system_metadata)
    assert persisted.system_metadata["custom_flag"] == "preserved"
    assert persisted.user_metadata == {"hierarchy_role": "business-role"}


def test_unified_roundtrips_hierarchy_and_vectors_together() -> None:
    stores = create_test_stores()
    plugins = create_test_plugins()
    domain = make_storage(kv=stores["kv"]).domain_store()
    builder = UnifiedIndexBuilder(
        domain,
        vector_enabled=True,
        chunker=plugins["chunker"],
        embedder=plugins["embedder"],
    )
    unit = _tree_unit()

    builder.build([unit])

    chunks = plugins["chunker"].chunk(
        text=unit.content, unit_id=unit.id, metadata={"tier": unit.tier.value}
    )
    vectors = plugins["embedder"].embed([chunk.text for chunk in chunks])
    expected_vectors = [
        ChunkVector(id=chunk.id, seq=chunk.seq, vector=vector)
        for chunk, vector in zip(chunks, vectors)
    ]
    persisted = domain.get(unit.scope, [unit.id])[0]
    assert expected_vectors, "测试文本应实际产出 chunk 向量"
    assert persisted.vectors == expected_vectors
    assert persisted.hierarchy == unit.hierarchy
    assert {key: persisted.system_metadata[key] for key in _TREE_METADATA} == _TREE_METADATA

    persisted.segments = [Segment(content="updated project implementation details")]
    persisted.hierarchy = HierarchyRef()
    builder.update([persisted])

    updated = domain.get(unit.scope, [unit.id])[0]
    chunks = plugins["chunker"].chunk(
        text=persisted.content, unit_id=persisted.id, metadata={"tier": persisted.tier.value}
    )
    vectors = plugins["embedder"].embed([chunk.text for chunk in chunks])
    updated_vectors = [
        ChunkVector(id=chunk.id, seq=chunk.seq, vector=vector)
        for chunk, vector in zip(chunks, vectors)
    ]
    assert updated_vectors != expected_vectors, "文本改写应重新向量化"
    assert updated.vectors == updated_vectors
    assert updated.hierarchy.is_empty
    assert not set(_TREE_METADATA).intersection(updated.system_metadata)


@pytest.mark.parametrize("kind", ["fulltext", "vector"])
@pytest.mark.parametrize("clear_hierarchy", [False, True], ids=["change_kind", "leave_tree"])
def test_index_update_discards_unified_hierarchy_copies(
    kind: str, clear_hierarchy: bool
) -> None:
    stores = create_test_stores()
    plugins = create_test_plugins()
    domain = make_storage(kv=stores["kv"]).domain_store()
    unified = UnifiedIndexBuilder(domain, vector_enabled=False)
    unit = _tree_unit()
    unit.system_metadata["custom_flag"] = "preserved"
    business_metadata = {key: f"business-{key}" for key in HIERARCHY_INDEX_KEYS}
    unit.user_metadata.update(business_metadata)
    unified.build([unit])
    persisted = domain.get(unit.scope, [unit.id])[0]
    assert {key: persisted.system_metadata[key] for key in _TREE_METADATA} == _TREE_METADATA

    manager = CompositeStoreManager(kv=stores["kv"], **{kind: stores[kind]})
    if kind == "fulltext":
        builder = FulltextIndexBuilder(manager)
    else:
        builder = VectorIndexBuilder(manager, plugins["chunker"], plugins["embedder"])
    builder.build([persisted])

    if clear_hierarchy:
        persisted.hierarchy = HierarchyRef()
        expected = {}
    else:
        persisted.hierarchy = HierarchyRef(kind=HierarchyKind.TOPIC, role=HierarchyRole.NODE)
        expected = {
            "hierarchy_kind": "topic",
            "hierarchy_role": "node",
            "hierarchy_status": "active",
            "parent_id": "",
        }
    builder.update([persisted])

    record_ids = (
        [unit.id]
        if kind == "fulltext"
        else json.loads(stores["kv"].get(unit.scope, f"/index/chunks/{unit.id}"))
    )
    records = stores[kind].get(unit.scope, record_ids)
    assert records, "跨 builder 更新后必须保留实际内容索引"
    system_copies = {f"system_metadata.{key}" for key in HIERARCHY_INDEX_KEYS}
    for record in records:
        projected = {
            key: value for key, value in record.metadata.items() if key in HIERARCHY_INDEX_KEYS
        }
        assert projected == expected, "结构裸字段必须只取当前 HierarchyRef"
        assert not system_copies.intersection(record.metadata), "旧系统副本不得进入检索记录"
        assert record.metadata["system_metadata.custom_flag"] == "preserved"
        assert {
            key: record.metadata[f"user_metadata.{key}"] for key in HIERARCHY_INDEX_KEYS
        } == business_metadata
    assert {key: persisted.system_metadata[key] for key in _TREE_METADATA} == _TREE_METADATA
