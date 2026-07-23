"""Elasticsearch fulltext store 的索引 mapping 单测试。"""

from __future__ import annotations

import pytest

from storage.fulltext_impl.elasticsearch_fulltext import ElasticsearchFulltextStore

pytestmark = pytest.mark.unit


class _FakeIndices:
    def __init__(self) -> None:
        self.created: dict | None = None

    @staticmethod
    def exists(*, index: str) -> bool:
        return False

    def create(self, **kwargs) -> None:
        self.created = kwargs


class _FakeClient:
    def __init__(self) -> None:
        self.indices = _FakeIndices()


def test_text_analyzer_is_written_to_index_mapping() -> None:
    store = ElasticsearchFulltextStore(index="memory_l0", text_analyzer="english")
    client = _FakeClient()
    store._client = client

    store._ensure_index()

    assert client.indices.created is not None
    text_mapping = client.indices.created["mappings"]["properties"]["text"]
    assert text_mapping == {"type": "text", "analyzer": "english"}
