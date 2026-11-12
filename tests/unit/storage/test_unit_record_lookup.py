# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 D16：后端按 ``metadata.unit_id`` 反查记录 id（``UNIT_RECORD_LOOKUP``）。

搬迁随后会清理源分片，反查漏一条就丢一条索引记录；这里锁住「能查全」的后端约束：
内存后端按精确 Scope 匹配，ES 先 refresh 再分页取全，Milvus 强一致且触顶报错。
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
    WhitespaceTokenizer,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.capability import StoreCapability, capabilities_of
from jiuwen_memory.storage.fulltext_impl import elasticsearch_fulltext as es_module
from jiuwen_memory.storage.fulltext_impl.elasticsearch_fulltext import (
    ElasticsearchFulltextStore,
)
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import (
    InMemoryFulltextStore,
)
from jiuwen_memory.storage.types import Document, VectorRecord
from jiuwen_memory.storage.vector_impl import milvus_vector as milvus_module
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore
from jiuwen_memory.storage.vector_impl.milvus_vector import MilvusVectorStore
from tests.support.whitebox import private, private_method, set_private

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u", session="s1")
OTHER_SESSION = Scope(org="acme", user="u", session="s2")


def _vector_store() -> InMemoryVectorStore:
    store = InMemoryVectorStore()
    records = []
    for record_id, unit_id in (
        ("u1", "u1"),
        ("u1-0", "u1"),
        ("u1-1", "u1"),
        ("u1-layer-l0", "u1"),
        ("u2", "u2"),
    ):
        records.append(
            VectorRecord(
                id=record_id, vector=[1.0, 0.0], metadata={"unit_id": unit_id}
            )
        )
    store.insert(SCOPE, records)
    store.insert(
        OTHER_SESSION,
        [VectorRecord(id="u1", vector=[1.0, 0.0], metadata={"unit_id": "u1"})],
    )
    return store


def _fulltext_store() -> InMemoryFulltextStore:
    store = InMemoryFulltextStore(WhitespaceTokenizer())
    store.insert(
        SCOPE,
        [
            Document(id="u1", text="alpha", metadata={"unit_id": "u1"}),
            Document(id="u1:l1", text="alpha", metadata={"unit_id": "u1"}),
            Document(id="u2", text="beta", metadata={"unit_id": "u2"}),
        ],
    )
    store.insert(OTHER_SESSION, [Document(id="u1", text="x", metadata={"unit_id": "u1"})])
    return store


def test_in_memory_vector_lookup_finds_chunks_and_layers_in_exact_scope() -> None:
    store = _vector_store()
    assert StoreCapability.UNIT_RECORD_LOOKUP in capabilities_of(store)
    found = store.record_ids_for_units(SCOPE, ["u1", "missing"])
    assert sorted(found["u1"]) == ["u1", "u1-0", "u1-1", "u1-layer-l0"]
    assert "missing" not in found
    assert store.record_ids_for_units(OTHER_SESSION, ["u1"]) == {"u1": ["u1"]}


def test_in_memory_fulltext_lookup_finds_layer_documents_in_exact_scope() -> None:
    store = _fulltext_store()
    assert StoreCapability.UNIT_RECORD_LOOKUP in capabilities_of(store)
    assert sorted(store.record_ids_for_units(SCOPE, ["u1"])["u1"]) == ["u1", "u1:l1"]
    assert store.record_ids_for_units(OTHER_SESSION, ["u1"]) == {"u1": ["u1"]}


class _FakeIndices:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def refresh(self, index: str) -> None:
        self._calls.append(f"refresh:{index}")


class _FakeEs:
    """按 ``from_`` / ``size`` 分页返回命中，记录调用顺序。"""

    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.calls: list[str] = []
        self.indices = _FakeIndices(self.calls)
        self._hits = hits

    def search(self, *, index: str, query: dict, from_: int, size: int, **_: Any) -> dict:
        self.calls.append(f"search:{from_}:{size}")
        return {"hits": {"hits": self._hits[from_:from_ + size]}}


def _es_with(hits: list[dict[str, Any]]) -> tuple[ElasticsearchFulltextStore, _FakeEs]:
    store = ElasticsearchFulltextStore(index="memory_fulltext")
    fake = _FakeEs(hits)
    resolved = private_method(store, "_resolved_hosts")()
    set_private(store, "_client", fake)
    set_private(
        store, "_client_hosts", tuple(resolved) if isinstance(resolved, list) else resolved
    )
    return store, fake


def _es_hit(doc_id: str, unit_id: str) -> dict[str, Any]:
    return {"_id": doc_id, "_source": {"metadata": {"unit_id": unit_id}}}


def test_es_lookup_refreshes_then_pages_until_exhausted(monkeypatch) -> None:
    monkeypatch.setattr(es_module, "_RECORD_LOOKUP_PAGE", 2)
    hits = [_es_hit(f"doc-{i}", "u1") for i in range(5)]
    store, fake = _es_with(hits)
    monkeypatch.setattr(store, "_logical_id", lambda doc_id: doc_id)

    found = store.record_ids_for_units(SCOPE, ["u1"])

    assert fake.calls[0] == "refresh:memory_fulltext", "near-real-time docs must be visible"
    assert fake.calls[1:] == ["search:0:2", "search:2:2", "search:4:2"]
    assert found == {"u1": [f"doc-{i}" for i in range(5)]}


def test_es_lookup_refuses_to_truncate_beyond_result_window(monkeypatch) -> None:
    monkeypatch.setattr(es_module, "_RECORD_LOOKUP_PAGE", 2)
    monkeypatch.setattr(es_module, "_RECORD_LOOKUP_WINDOW", 4)
    store, _ = _es_with([_es_hit(f"doc-{i}", "u1") for i in range(10)])
    monkeypatch.setattr(store, "_logical_id", lambda doc_id: doc_id)

    with pytest.raises(BackendError, match="exceeds"):
        store.record_ids_for_units(SCOPE, ["u1"])


class _FakeMilvus:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.kwargs: dict[str, Any] = {}
        self._rows = rows

    def query(self, collection: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.kwargs = kwargs
        return self._rows


def _milvus_with(rows: list[dict[str, Any]]) -> tuple[MilvusVectorStore, _FakeMilvus]:
    store = MilvusVectorStore(dim=2, consistency_level="Eventually")
    fake = _FakeMilvus(rows)
    set_private(store, "_client", fake)
    set_private(store, "_client_uri", private_method(store, "_resolved_uri")())
    return store, fake


def test_milvus_lookup_uses_strong_consistency_regardless_of_config() -> None:
    rows = [{"logical_id": "u1-0", "metadata": {"unit_id": "u1"}}]
    store, fake = _milvus_with(rows)

    assert store.record_ids_for_units(SCOPE, ["u1"]) == {"u1": ["u1-0"]}
    assert fake.kwargs["consistency_level"] == "Strong"
    assert fake.kwargs["limit"] == private(milvus_module, "_RECORD_LOOKUP_LIMIT")


def test_milvus_lookup_raises_when_query_limit_is_reached(monkeypatch) -> None:
    monkeypatch.setattr(milvus_module, "_RECORD_LOOKUP_LIMIT", 2)
    rows = [{"logical_id": f"u1-{i}", "metadata": {"unit_id": "u1"}} for i in range(2)]
    store, _ = _milvus_with(rows)

    with pytest.raises(BackendError, match="query limit"):
        store.record_ids_for_units(SCOPE, ["u1"])
