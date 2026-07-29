"""Milvus / Elasticsearch 完整 FilterExpr 编译器测试。"""

from __future__ import annotations

import pytest

from common.type_def import FilterClause, FilterGroup, FilterLogic, FilterOp, Scope, normalize
from storage.fulltext_impl.elasticsearch_fulltext import ElasticsearchFulltextStore
from storage.types import TextQuery, VectorQuery
from storage.vector_impl.milvus_vector import MilvusVectorStore

pytestmark = pytest.mark.unit


class RecordingMilvusClient:
    def __init__(self) -> None:
        self.last_filter = ""

    def search(self, *args, **kwargs):
        assert args, "Milvus search 应携带 collection 位置参数"
        self.last_filter = kwargs["filter"]
        return [[]]


class MilvusStoreHarness(MilvusVectorStore):
    def __init__(self) -> None:
        super().__init__(dim=3)
        self.recording_client = RecordingMilvusClient()

    @property
    def client(self):
        return self.recording_client


class RecordingElasticsearchClient:
    def __init__(self) -> None:
        self.last_query = {}

    def search(self, **kwargs):
        self.last_query = kwargs["query"]
        return {"hits": {"hits": []}}


class ElasticsearchStoreHarness(ElasticsearchFulltextStore):
    def __init__(self) -> None:
        super().__init__()
        self.recording_client = RecordingElasticsearchClient()

    @property
    def client(self):
        return self.recording_client


def _tree():
    return normalize(
        {
            "AND": [
                {"metadata.memory_type": "coding"},
                {
                    "OR": [
                        {"project": {"in": ["alpha", "beta"]}},
                        {"priority": {"gte": 8}},
                    ]
                },
                {"NOT": {"status": "archived"}},
                {"tags": {"contains": "work"}},
            ]
        }
    )


def _milvus_filter(filters, scope: Scope | None = None) -> str:
    store = MilvusStoreHarness()
    store.search(
        scope or Scope(),
        VectorQuery(vector=[1.0, 0.0, 0.0], filters=filters),
    )
    return store.recording_client.last_filter


def _elasticsearch_filter(filters):
    store = ElasticsearchStoreHarness()
    store.search(Scope(), TextQuery(text="query", filters=filters))
    return store.recording_client.last_query["bool"]["filter"][0]


def test_milvus_compiles_complete_boolean_tree() -> None:
    compiled = _milvus_filter(_tree())

    assert 'metadata["memory_type"] == "coding"' in compiled
    assert 'metadata["project"] in ["alpha", "beta"]' in compiled
    assert 'metadata["priority"] >= 8' in compiled
    assert " || " in compiled
    assert '(not (metadata["status"] == "archived"))' in compiled
    assert 'json_contains(metadata["tags"], "work")' in compiled
    assert 'metadata["metadata.memory_type"]' not in compiled


def test_milvus_combines_scope_and_complete_filter() -> None:
    compiled = _milvus_filter(_tree(), Scope(org="acme", user="alice"))

    assert 'scope_org == "acme"' in compiled
    assert 'scope_user == "alice"' in compiled
    assert " || " in compiled
    assert "(not " in compiled


def test_elasticsearch_compiles_complete_boolean_tree() -> None:
    compiled = _elasticsearch_filter(_tree())

    assert compiled == {
        "bool": {
            "filter": [
                {"term": {"metadata.memory_type": "coding"}},
                {
                    "bool": {
                        "should": [
                            {"terms": {"metadata.project": ["alpha", "beta"]}},
                            {"range": {"metadata.priority": {"gte": 8}}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                {
                    "bool": {
                        "must_not": [{"term": {"metadata.status": "archived"}}],
                    }
                },
                {"term": {"metadata.tags": "work"}},
            ]
        }
    }


@pytest.mark.parametrize(
    "op,value,fragment",
    [
        (FilterOp.EQ, "x", 'metadata["f"] == "x"'),
        (FilterOp.NE, "x", 'metadata["f"] != "x"'),
        (FilterOp.IN, ["x", "y"], 'metadata["f"] in ["x", "y"]'),
        (FilterOp.NOT_IN, ["x"], 'metadata["f"] not in ["x"]'),
        (FilterOp.GT, 1, 'metadata["f"] > 1'),
        (FilterOp.GTE, 1, 'metadata["f"] >= 1'),
        (FilterOp.LT, 1, 'metadata["f"] < 1'),
        (FilterOp.LTE, 1, 'metadata["f"] <= 1'),
        (FilterOp.CONTAINS, "x", 'json_contains(metadata["f"], "x")'),
    ],
)
def test_milvus_compiles_every_leaf_operator(op: FilterOp, value, fragment: str) -> None:
    assert _milvus_filter(normalize(FilterClause("f", op, value))) == fragment


def test_elasticsearch_compiles_nested_not_group() -> None:
    expr = FilterGroup(
        FilterLogic.NOT,
        [
            FilterGroup(
                FilterLogic.OR,
                [
                    FilterClause("project", FilterOp.EQ, "alpha"),
                    FilterClause("project", FilterOp.EQ, "beta"),
                ],
            )
        ],
    )

    compiled = _elasticsearch_filter(normalize(expr))

    assert compiled == {
        "bool": {
            "must_not": [
                {
                    "bool": {
                        "should": [
                            {"term": {"metadata.project": "alpha"}},
                            {"term": {"metadata.project": "beta"}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            ]
        }
    }
