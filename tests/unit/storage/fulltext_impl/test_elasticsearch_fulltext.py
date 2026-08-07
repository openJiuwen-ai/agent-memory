"""Elasticsearch fulltext store 的索引 mapping 单测试。"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from common.type_def import Scope
from storage.fulltext_impl.elasticsearch_fulltext import ElasticsearchFulltextStore
from storage.types import Document, TextQuery

pytestmark = pytest.mark.unit


class _FakeIndices:
    def __init__(self, *, exists: bool = False) -> None:
        self._exists = exists
        self.created: dict | None = None
        self.updated: dict | None = None
        self.analyzed: dict | None = None
        self.analyze_tokens: list[str] = []

    def exists(self, *, index: str) -> bool:
        return self._exists

    def create(self, **kwargs) -> None:
        self.created = kwargs

    def put_mapping(self, **kwargs) -> None:
        self.updated = kwargs

    def analyze(self, **kwargs) -> dict:
        self.analyzed = kwargs
        return {"tokens": [{"token": token} for token in self.analyze_tokens]}


class _FakeClient:
    def __init__(self, *, index_exists: bool = False) -> None:
        self.indices = _FakeIndices(exists=index_exists)
        self.documents: dict[str, dict] = {}
        self.searches: list[dict] = []

    def bulk(self, *, operations: list[dict], refresh: str) -> dict:
        for offset in range(0, len(operations), 2):
            action = operations[offset]["create"]
            self.documents[action["_id"]] = operations[offset + 1]
        return {"errors": False}

    def mget(self, *, index: str, ids: list[str]) -> dict:
        return {
            "docs": [
                (
                    {"_id": doc_id, "found": True, "_source": self.documents[doc_id]}
                    if doc_id in self.documents
                    else {"_id": doc_id, "found": False}
                )
                for doc_id in ids
            ]
        }

    def search(self, **kwargs) -> dict:
        self.searches.append(kwargs)
        return {"hits": {"hits": []}}


def test_text_analyzer_is_written_to_index_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()

    def create_client(*_args: object, **_kwargs: object) -> _FakeClient:
        return client

    elasticsearch = ModuleType("elasticsearch")
    setattr(elasticsearch, "Elasticsearch", create_client)
    monkeypatch.setitem(sys.modules, "elasticsearch", elasticsearch)

    store = ElasticsearchFulltextStore(index="memory_l0", text_analyzer="english")
    assert store.client is client

    assert client.indices.created is not None
    text_mapping = client.indices.created["mappings"]["properties"]["text"]
    assert text_mapping == {"type": "text", "analyzer": "english"}
    array_marker = client.indices.created["mappings"]["properties"]["metadata_array_fields"]
    assert array_marker == {"type": "keyword"}
    metadata_mapping = client.indices.created["mappings"]["properties"]["metadata"]
    tags_mapping = metadata_mapping["properties"]["tags"]
    assert tags_mapping == {"type": "keyword"}


def test_existing_index_gets_array_marker_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(index_exists=True)

    def create_client(*_args: object, **_kwargs: object) -> _FakeClient:
        return client

    elasticsearch = ModuleType("elasticsearch")
    setattr(elasticsearch, "Elasticsearch", create_client)
    monkeypatch.setitem(sys.modules, "elasticsearch", elasticsearch)

    store = ElasticsearchFulltextStore(index="memory_l0")
    assert store.client is client
    assert client.indices.created is None
    assert client.indices.updated == {
        "index": "memory_l0",
        "properties": {
            "metadata_array_fields": {"type": "keyword"},
            "metadata": {"properties": {"tags": {"type": "keyword"}}},
        },
    }


def test_source_records_array_metadata_keys_without_exposing_them_as_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient()

    def create_client(*_args: object, **_kwargs: object) -> _FakeClient:
        return client

    elasticsearch = ModuleType("elasticsearch")
    setattr(elasticsearch, "Elasticsearch", create_client)
    monkeypatch.setitem(sys.modules, "elasticsearch", elasticsearch)

    store = ElasticsearchFulltextStore()
    scope = Scope(org="acme")
    metadata = {"project": "alpha", "tags": ["work", "urgent"]}
    store.insert(
        scope,
        [Document(id="a", text="doc", metadata=metadata)],
    )

    source = next(iter(client.documents.values()))
    assert source["metadata"] == metadata
    assert source["metadata_array_fields"] == ["tags"]
    assert store.get(scope, ["a"])[0].metadata == metadata


def test_search_filters_stopwords_from_es_analyzed_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient()
    client.indices.analyze_tokens = ["这", "本书", "是", "讲", "什么", "的"]

    def create_client(*_args: object, **_kwargs: object) -> _FakeClient:
        return client

    elasticsearch = ModuleType("elasticsearch")
    setattr(elasticsearch, "Elasticsearch", create_client)
    monkeypatch.setitem(sys.modules, "elasticsearch", elasticsearch)

    store = ElasticsearchFulltextStore(index="memory_zh")
    result = store.search(Scope(org="acme"), TextQuery(text="这本书是讲什么的"))

    assert result == []
    assert client.indices.analyzed == {
        "index": "memory_zh",
        "field": "text",
        "text": "这本书是讲什么的",
    }
    keyword_query = client.searches[0]["query"]["bool"]["must"][0]
    assert keyword_query == {
        "bool": {
            "should": [
                {"term": {"text": "本书"}},
                {"term": {"text": "讲"}},
                {"term": {"text": "什么"}},
            ],
            "minimum_should_match": 1,
        }
    }


def test_search_returns_empty_when_query_contains_only_stopwords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient()
    client.indices.analyze_tokens = ["这", "是", "的"]

    def create_client(*_args: object, **_kwargs: object) -> _FakeClient:
        return client

    elasticsearch = ModuleType("elasticsearch")
    setattr(elasticsearch, "Elasticsearch", create_client)
    monkeypatch.setitem(sys.modules, "elasticsearch", elasticsearch)

    store = ElasticsearchFulltextStore(index="memory_zh")
    result = store.search(Scope(org="acme"), TextQuery(text="这是的"))

    assert result == []
    assert client.searches == []
