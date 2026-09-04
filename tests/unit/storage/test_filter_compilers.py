"""Milvus / Elasticsearch / PostgreSQL 完整 FilterExpr 编译器测试。"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    Scope,
    normalize,
)
from jiuwen_memory.storage._pg import compile_pg_filter, pg_scope_clause
from jiuwen_memory.storage.fulltext_impl.elasticsearch_fulltext import ElasticsearchFulltextStore
from jiuwen_memory.storage.types import Document, TextQuery, VectorQuery
from jiuwen_memory.storage.vector_impl.milvus_vector import MilvusVectorStore

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

    def _analyze_query(self, text: str) -> list[str]:
        return [text]

    def serialize_roundtrip(
        self, scope: Scope, document: Document
    ) -> tuple[dict[str, Any], Document]:
        source = self._source(scope, document)
        return source, self._to_document("physical-id", source)


def _tree():
    return normalize(
        {
            "AND": [
                {"system_metadata.memory_type": "coding"},
                {
                    "OR": [
                        {"user_metadata.project": {"in": ["alpha", "beta"]}},
                        {"user_metadata.priority": {"gte": 8}},
                    ]
                },
                {"NOT": {"user_metadata.status": "archived"}},
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


def _es_scalar_match(field: str, value_query: dict) -> dict:
    key = field.removeprefix("metadata.")
    return {
        "bool": {
            "filter": [value_query],
            "must_not": [{"term": {"metadata_array_fields": key}}],
        }
    }


def test_elasticsearch_expands_metadata_namespaces_into_object_paths() -> None:
    store = ElasticsearchStoreHarness()
    document = Document(
        id="u1",
        text="memory",
        metadata={
            "system_metadata.memory_type": "coding",
            "user_metadata.project": "alpha",
            "unit_id": "u1",
        },
    )

    source, restored = store.serialize_roundtrip(Scope(), document)

    assert source["metadata"] == {
        "system_metadata": {"memory_type": "coding"},
        "user_metadata": {"project": "alpha"},
        "unit_id": "u1",
    }
    assert restored == document


def test_milvus_compiles_complete_boolean_tree() -> None:
    compiled = _milvus_filter(_tree())

    assert 'metadata["system_metadata.memory_type"] == "coding"' in compiled
    assert 'metadata["user_metadata.project"] in ["alpha", "beta"]' in compiled
    assert 'metadata["user_metadata.priority"] >= 8' in compiled
    assert " || " in compiled
    assert '(not (metadata["user_metadata.status"] == "archived"))' in compiled
    assert 'json_contains(metadata["tags"], "work")' in compiled


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
                _es_scalar_match(
                    "metadata.system_metadata.memory_type",
                    {"term": {"metadata.system_metadata.memory_type": "coding"}},
                ),
                {
                    "bool": {
                        "should": [
                            _es_scalar_match(
                                "metadata.user_metadata.project",
                                {"terms": {"metadata.user_metadata.project": ["alpha", "beta"]}},
                            ),
                            _es_scalar_match(
                                "metadata.user_metadata.priority",
                                {"range": {"metadata.user_metadata.priority": {"gte": 8}}},
                            ),
                        ],
                        "minimum_should_match": 1,
                    }
                },
                {
                    "bool": {
                        "must_not": [
                            _es_scalar_match(
                                "metadata.user_metadata.status",
                                {"term": {"metadata.user_metadata.status": "archived"}},
                            )
                        ],
                    }
                },
                {
                    "bool": {
                        "filter": [
                            {"term": {"metadata.tags": "work"}},
                            {"term": {"metadata_array_fields": "tags"}},
                        ]
                    }
                },
            ]
        }
    }


@pytest.mark.parametrize(
    "op,value,fragment",
    [
        (FilterOp.EQ, "x", 'metadata["user_metadata.f"] == "x"'),
        (FilterOp.NE, "x", 'metadata["user_metadata.f"] != "x"'),
        (FilterOp.IN, ["x", "y"], 'metadata["user_metadata.f"] in ["x", "y"]'),
        (FilterOp.NOT_IN, ["x"], 'metadata["user_metadata.f"] not in ["x"]'),
        (FilterOp.GT, 1, 'metadata["user_metadata.f"] > 1'),
        (FilterOp.GTE, 1, 'metadata["user_metadata.f"] >= 1'),
        (FilterOp.LT, 1, 'metadata["user_metadata.f"] < 1'),
        (FilterOp.LTE, 1, 'metadata["user_metadata.f"] <= 1'),
        (FilterOp.CONTAINS, "x", 'json_contains(metadata["user_metadata.f"], "x")'),
    ],
)
def test_milvus_compiles_every_leaf_operator(op: FilterOp, value, fragment: str) -> None:
    assert _milvus_filter(normalize(FilterClause("user_metadata.f", op, value))) == fragment


def test_elasticsearch_compiles_nested_not_group() -> None:
    expr = FilterGroup(
        FilterLogic.NOT,
        [
            FilterGroup(
                FilterLogic.OR,
                [
                    FilterClause("user_metadata.project", FilterOp.EQ, "alpha"),
                    FilterClause("user_metadata.project", FilterOp.EQ, "beta"),
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
                            _es_scalar_match(
                                "metadata.user_metadata.project",
                                {"term": {"metadata.user_metadata.project": "alpha"}},
                            ),
                            _es_scalar_match(
                                "metadata.user_metadata.project",
                                {"term": {"metadata.user_metadata.project": "beta"}},
                            ),
                        ],
                        "minimum_should_match": 1,
                    }
                }
            ]
        }
    }


def test_elasticsearch_distinguishes_scalar_equality_from_array_membership() -> None:
    eq = _elasticsearch_filter(FilterClause("user_metadata.field", FilterOp.EQ, "x"))
    contains = _elasticsearch_filter(FilterClause("user_metadata.field", FilterOp.CONTAINS, "x"))

    assert eq == _es_scalar_match(
        "metadata.user_metadata.field",
        {"term": {"metadata.user_metadata.field": "x"}},
    )
    assert contains == {
        "bool": {
            "filter": [
                {"term": {"metadata.user_metadata.field": "x"}},
                {"term": {"metadata_array_fields": "user_metadata.field"}},
            ]
        }
    }


def test_elasticsearch_range_excludes_array_fields() -> None:
    """Lucene 的 range 对多值字段任一成员命中即匹配，须限定标量。

    真源复核对数组 + 范围算子判否，pg 用 ``jsonb_typeof='number'`` 守卫；ES 若放行
    会比另两处宽松，同一谓词给出不同候选集。
    """
    compiled = _elasticsearch_filter(FilterClause("user_metadata.score", FilterOp.GT, 5))

    assert compiled == _es_scalar_match(
        "metadata.user_metadata.score",
        {"range": {"metadata.user_metadata.score": {"gt": 5}}},
    )


def test_postgres_compiles_complete_boolean_tree_with_parameters() -> None:
    fragment, params = compile_pg_filter(_tree())

    assert "metadata @> jsonb_build_object(%s::text" in fragment
    assert "::numeric >= %s::numeric" in fragment
    assert " OR " in fragment
    assert "NOT COALESCE" in fragment
    assert "system_metadata.memory_type" in params
    assert "metadata.system_metadata.memory_type" not in params
    assert {"coding", "alpha", "beta", 8, "archived", "work"} <= set(params)


@pytest.mark.parametrize(
    "op,value,expected",
    [
        (FilterOp.EQ, "x", "COALESCE"),
        (FilterOp.NE, "x", "NOT COALESCE"),
        (FilterOp.IN, ["x", "y"], " OR "),
        (FilterOp.NOT_IN, ["x"], "NOT COALESCE"),
        (FilterOp.GT, 1, "::numeric > %s::numeric"),
        (FilterOp.GTE, 1, "::numeric >= %s::numeric"),
        (FilterOp.LT, 1, "::numeric < %s::numeric"),
        (FilterOp.LTE, 1, "::numeric <= %s::numeric"),
        (FilterOp.CONTAINS, "x", "jsonb_build_array"),
    ],
)
def test_postgres_compiles_every_leaf_operator(
    op: FilterOp, value, expected: str
) -> None:
    fragment, params = compile_pg_filter(normalize(FilterClause("user_metadata.f", op, value)))

    assert expected in fragment
    assert "user_metadata.f" in params


def test_postgres_distinguishes_scalar_equality_from_array_membership() -> None:
    eq_fragment, eq_params = compile_pg_filter(FilterClause("user_metadata.f", FilterOp.EQ, "x"))
    contains_fragment, contains_params = compile_pg_filter(
        FilterClause("user_metadata.f", FilterOp.CONTAINS, "x")
    )
    in_fragment, _ = compile_pg_filter(FilterClause("user_metadata.f", FilterOp.IN, ["x", "y"]))

    assert "jsonb_build_array" not in eq_fragment
    assert "jsonb_typeof" not in eq_fragment
    assert eq_params == ["user_metadata.f", "x"]
    assert "jsonb_typeof(metadata->%s) = 'array'" in contains_fragment
    assert "jsonb_build_array" in contains_fragment
    assert contains_params == ["user_metadata.f", "user_metadata.f", "x"]
    assert "jsonb_build_array" not in in_fragment


def test_postgres_filter_values_never_enter_sql_text() -> None:
    hostile = "x'); DROP TABLE memories; --"
    fragment, params = compile_pg_filter(
        normalize(FilterClause("user_metadata.hostile", FilterOp.EQ, hostile))
    )

    assert hostile not in fragment
    assert "hostile" not in fragment
    assert hostile in params
    assert "user_metadata.hostile" in params


def test_postgres_scope_clause_uses_five_dimensions_for_exact_crud() -> None:
    fragment, params = pg_scope_clause(
        Scope(org="acme", space="prod", user="alice", agent="bot", session="s1"),
        exact=True,
    )

    assert fragment == (
        "scope_org = %s AND scope_space = %s AND scope_user = %s "
        "AND scope_agent = %s AND scope_session = %s"
    )
    assert params == ["acme", "prod", "alice", "bot", "s1"]


def test_postgres_hierarchical_scope_keeps_empty_space_boundary() -> None:
    fragment, params = pg_scope_clause(Scope(org="acme"), exact=False)

    assert fragment == "scope_org = %s AND scope_space = %s"
    assert params == ["acme", ""]
