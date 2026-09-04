"""MilvusGraphFusionStore adapter tests that do not require external Milvus or graph services."""

from __future__ import annotations

import pytest

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.fusion_impl.milvus_graph_fusion import MilvusGraphFusionStore
from jiuwen_memory.storage.types import FusionQuery

pytestmark = pytest.mark.unit


class _RecordingVectorStore:
    last_instance = None

    def __init__(self, **kwargs) -> None:
        self.query = None
        type(self).last_instance = self

    def search(self, scope, query):
        self.query = query
        return []


def _no_neighbors(self, scope, seeds):
    return {}


def test_milvus_graph_fusion_forwards_runtime_extension_identity_to_vector_adapter(
    tmp_path, monkeypatch
) -> None:
    """Adapter boundary: FusionQuery forwards live extension objects to its internal VectorQuery.

    This does not exercise Milvus I/O or graph traversal. Fusion is a storage adapter rather than a
    Pipeline recall channel, so those behaviors belong to their backend and retrieval tests.
    """
    marker = object()
    monkeypatch.setattr(
        "jiuwen_memory.storage.fusion_impl.milvus_graph_fusion.MilvusVectorStore",
        _RecordingVectorStore,
    )
    monkeypatch.setattr(MilvusGraphFusionStore, "_expand_neighbors", _no_neighbors)
    store = MilvusGraphFusionStore(working_dir=str(tmp_path), dim=2)

    store.search(
        Scope(org="acme", user="u1"),
        FusionQuery(vector=[0.1, 0.2], extensions={"fusion_runtime": marker}),
    )

    vector_store = _RecordingVectorStore.last_instance
    assert vector_store is not None
    assert vector_store.query.extensions["fusion_runtime"] is marker
