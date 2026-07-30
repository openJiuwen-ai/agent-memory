"""Elasticsearch fulltext store 的索引 mapping 单测试。"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from common.type_def import Scope
from storage.fulltext_impl.elasticsearch_fulltext import ElasticsearchFulltextStore
from storage.types import Document

pytestmark = pytest.mark.unit


class _FakeIndices:
    def __init__(self, *, exists: bool = False) -> None:
        self._exists = exists
        self.created: dict | None = None
        self.updated: dict | None = None

    def exists(self, *, index: str) -> bool:
        return self._exists

    def create(self, **kwargs) -> None:
        self.created = kwargs

    def put_mapping(self, **kwargs) -> None:
        self.updated = kwargs


class _FakeClient:
    def __init__(self, *, index_exists: bool = False) -> None:
        self.indices = _FakeIndices(exists=index_exists)


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
        "properties": {"metadata_array_fields": {"type": "keyword"}},
    }


def test_source_records_array_metadata_keys_without_exposing_them_as_metadata() -> None:
    store = ElasticsearchFulltextStore()

    source = store._source(
        Scope(org="acme"),
        Document(
            id="a",
            text="doc",
            metadata={"project": "alpha", "tags": ["work", "urgent"]},
        ),
    )

    assert source["metadata"] == {"project": "alpha", "tags": ["work", "urgent"]}
    assert source["metadata_array_fields"] == ["tags"]
    assert store._to_document("physical-id", source).metadata == source["metadata"]
