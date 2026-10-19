"""树事实仅持久化，不进入索引与召回披露正文。"""

import json
from copy import deepcopy

import pytest

from jiuwen_memory.common.type_def import TreeFact, TreeMemoryData, memory_key
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.index_builder_impl._index_ops import index_metadata
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.construction.index_builder_impl.unified_index_builder import UnifiedIndexBuilder
from jiuwen_memory.retrieval.discloser_impl.truncating_discloser import TruncatingDiscloser
from jiuwen_memory.retrieval.types import DisclosureLevel, ParsedQuery, RecallChannel, ScoredUnit
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from tests.conftest import make_storage
from tests.unit.construction.fixtures import (
    create_test_plugins,
    create_test_stores,
    create_test_unit,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("kind", ["hybrid", "unified"])
def test_facts_roundtrip_without_entering_embedding_metadata_or_answer(kind, monkeypatch) -> None:
    unit = create_test_unit("parent", "Maya started a fashion internship.")
    hidden = "FACT_ONLY_SENTINEL 2023-05-27 interview accepted with Rob."
    unit.tree_memory = TreeMemoryData([TreeFact("source", deepcopy(unit.scope), hidden)])
    stores, plugins = create_test_stores(), create_test_plugins()
    calls = []
    embed = plugins["embedder"].embed

    def record_embedding(texts):
        calls.extend(texts)
        return embed(texts)

    monkeypatch.setattr(plugins["embedder"], "embed", record_embedding)
    if kind == "hybrid":
        manager = CompositeStoreManager(**stores)
        builder = HybridIndexBuilder(manager, plugins["chunker"], plugins["embedder"])
    else:
        domain = make_storage(kv=stores["kv"]).domain_store()
        builder = UnifiedIndexBuilder(domain, vector_enabled=True,
                                      chunker=plugins["chunker"], embedder=plugins["embedder"])
    builder.build([unit])
    restored = loads(stores["kv"].get(unit.scope, memory_key(unit.id)))
    assert restored.tree_memory == unit.tree_memory
    assert calls and all("FACT_ONLY_SENTINEL" not in text for text in calls)
    assert restored.content == unit.content
    assert "FACT_ONLY_SENTINEL" not in json.dumps(index_metadata(restored, layer="l2"))
    assert "tree_memory" not in json.dumps(index_metadata(restored, layer="l2"))
    if kind == "hybrid":
        document = stores["fulltext"].get(unit.scope, [unit.id])[0]
        assert document.text == unit.content
        chunk_ids = json.loads(stores["kv"].get(unit.scope, f"/index/chunks/{unit.id}"))
        records = stores["vector"].get(unit.scope, chunk_ids)
        assert records
        assert all("FACT_ONLY_SENTINEL" not in json.dumps(r.metadata) for r in records)
    else:
        assert restored.vectors

    item = TruncatingDiscloser().disclose(
        ParsedQuery(raw="internship"), [ScoredUnit(unit.id, 1.0, RecallChannel.VECTOR)],
        {unit.id: restored}, DisclosureLevel.L2,
    )[0]
    assert item.content == unit.content
    assert "FACT_ONLY_SENTINEL" not in item.content
