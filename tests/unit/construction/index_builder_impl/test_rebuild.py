"""Rebuild only projections: actual SQLite truth stays byte-identical."""


import pytest

from jiuwen_memory.common.type_def import Scope, memory_key
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode, TextQuery
from tests.unit.construction.fixtures import (
    create_test_plugins,
    create_test_stores,
    create_test_unit,
)

pytestmark = pytest.mark.unit


def _builder(kv):
    stores, plugins = create_test_stores(), create_test_plugins()
    builder = HybridIndexBuilder(
        CompositeStoreManager(kv=kv, vector=stores["vector"], fulltext=stores["fulltext"]),
        chunker=plugins["chunker"], embedder=plugins["embedder"],
    )
    return builder, stores


def test_retrieval_only_recovers_recall_without_rewriting_truth(tmp_path):
    kv = SQLiteKVStore(str(tmp_path / "truth.db"))
    builder, stores = _builder(kv)
    scope = Scope(org="o", space="s")
    unit = create_test_unit("old", "Python evidence", scope=scope)
    try:
        builder.build([unit])
        original = kv.get(scope, memory_key(unit.id))
        builder.remove([unit], mode=IndexRemoveMode.SOFT)
        assert not stores["fulltext"].search(scope, TextQuery(text="Python", top_k=10))
        builder.build([unit], mode=IndexWriteMode.RETRIEVAL_ONLY)
        assert kv.get(scope, memory_key(unit.id)) == original
        hits = stores["fulltext"].search(scope, TextQuery(text="Python", top_k=10))
        assert [hit.id for hit in hits] == [unit.id]
    finally:
        kv.close()
