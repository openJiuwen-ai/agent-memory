# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for backend FilterGroup renderers — Milvus / Chroma / ES / Gauss.

Covers the following cases:
  - EQ / NE / AND / OR across each backend
  - nested group rendering
  - empty FilterGroup rejection
  - nesting depth cap
"""
# Renderer methods are intentionally exercised through their internal
# ``_render_filters`` entry point to keep these unit tests hermetic and free
# of backend network IO.
# pylint: disable=protected-access
import pytest

from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import BaseError
from jiuwen_memory.foundation.store.filter_dsl import (
    FilterCondition,
    FilterGroup,
    FilterLogic,
    FilterOperator,
    MAX_NESTING_DEPTH,
)


# ---------------------------------------------------------------------------
# Milvus render
# ---------------------------------------------------------------------------

def _milvus():
    from jiuwen_memory.foundation.store.vector.milvus_vector_store import MilvusVectorStore
    return MilvusVectorStore(milvus_uri="http://localhost:19530")


class TestMilvusRender:
    @staticmethod
    def test_none_returns_none():
        assert _milvus()._render_filters(None) is None

    @staticmethod
    def test_single_eq():
        expr = _milvus()._render_filters(FilterGroup(conditions=[
            FilterCondition(field="category", op=FilterOperator.EQ, value="work"),
        ]))
        assert expr == 'category == "work"'

    @staticmethod
    def test_single_ne_with_bool():
        expr = _milvus()._render_filters(FilterGroup(conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
        ]))
        assert expr == "blacklisted != true"

    @staticmethod
    def test_and_group():
        g = FilterGroup(logic=FilterLogic.AND, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
            FilterCondition(field="b", op=FilterOperator.NE, value=2),
        ])
        assert _milvus()._render_filters(g) == "a == 1 && b != 2"

    @staticmethod
    def test_or_group():
        g = FilterGroup(logic=FilterLogic.OR, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value="x"),
            FilterCondition(field="a", op=FilterOperator.EQ, value="y"),
        ])
        assert _milvus()._render_filters(g) == 'a == "x" || a == "y"'

    @staticmethod
    def test_nested_group_renders_parentheses():
        inner = FilterGroup(logic=FilterLogic.OR, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value="x"),
            FilterCondition(field="a", op=FilterOperator.EQ, value="y"),
        ])
        outer = FilterGroup(conditions=[
            inner,
            FilterCondition(field="b", op=FilterOperator.NE, value=2),
        ])
        assert _milvus()._render_filters(outer) == '(a == "x" || a == "y") && b != 2'

    @staticmethod
    def test_empty_group_rejected():
        with pytest.raises(BaseError) as exc:
            _milvus()._render_filters(FilterGroup())
        assert exc.value.status == StatusCode.MEMORY_FILTER_FORMAT_ERROR

    @staticmethod
    def test_excessive_nesting_rejected():
        # Build a group at the maximum legal nesting depth (MAX_NESTING_DEPTH+1
        # nested levels including the top-level group) — this must construct OK.
        # Going one level deeper must be rejected by the depth-cap validator.
        raw = FilterCondition(field="f", value=1).model_dump()
        # MAX_NESTING_DEPTH+1 nested groups is the boundary (top-level is depth=0,
        # MAX_NESTING_DEPTH is the deepest allowed; +1 makes it reject).
        for _ in range(MAX_NESTING_DEPTH + 2):
            raw = {"conditions": [raw]}
        with pytest.raises(Exception):  # noqa: PT011 - pydantic ValidationError
            FilterGroup.model_validate(raw)

    @staticmethod
    def test_null_value_rendered():
        expr = _milvus()._render_filters(FilterGroup(conditions=[
            FilterCondition(field="x", op=FilterOperator.NE, value=None),
        ]))
        assert expr == "x != null"


# ---------------------------------------------------------------------------
# Chroma render
# ---------------------------------------------------------------------------

def _chroma():
    from jiuwen_memory.foundation.store.vector.chroma_vector_store import ChromaVectorStore
    return ChromaVectorStore()


class TestChromaRender:
    @staticmethod
    def test_none_returns_none():
        assert _chroma()._render_filters(None) is None

    @staticmethod
    def test_single_eq():
        where = _chroma()._render_filters(FilterGroup(conditions=[
            FilterCondition(field="category", op=FilterOperator.EQ, value="work"),
        ]))
        assert where == {"category": {"$eq": "work"}}

    @staticmethod
    def test_single_ne():
        where = _chroma()._render_filters(FilterGroup(conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
        ]))
        # Chroma has no ``$ne``; NE renders as ``$nin: [value]``.
        assert where == {"blacklisted": {"$nin": [True]}}

    @staticmethod
    def test_and_group():
        g = FilterGroup(logic=FilterLogic.AND, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
            FilterCondition(field="b", op=FilterOperator.NE, value=2),
        ])
        where = _chroma()._render_filters(g)
        assert where == {"$and": [{"a": {"$eq": 1}}, {"b": {"$nin": [2]}}]}

    @staticmethod
    def test_or_group():
        g = FilterGroup(logic=FilterLogic.OR, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value="x"),
            FilterCondition(field="a", op=FilterOperator.EQ, value="y"),
        ])
        where = _chroma()._render_filters(g)
        assert where == {"$or": [{"a": {"$eq": "x"}}, {"a": {"$eq": "y"}}]}

    @staticmethod
    def test_nested_group_renders_clauses():
        inner = FilterGroup(logic=FilterLogic.OR, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value="x"),
            FilterCondition(field="a", op=FilterOperator.EQ, value="y"),
        ])
        outer = FilterGroup(conditions=[
            inner,
            FilterCondition(field="b", op=FilterOperator.NE, value=2),
        ])
        where = _chroma()._render_filters(outer)
        assert where == {
            "$and": [
                {"$or": [{"a": {"$eq": "x"}}, {"a": {"$eq": "y"}}]},
                {"b": {"$nin": [2]}},
            ]
        }

    @staticmethod
    def test_empty_group_rejected():
        with pytest.raises(BaseError):
            _chroma()._render_filters(FilterGroup())


# ---------------------------------------------------------------------------
# Elasticsearch render
# ---------------------------------------------------------------------------

def _es():
    from jiuwen_memory.foundation.store.vector.es_vector_store import ElasticsearchVectorStore
    return ElasticsearchVectorStore()


class TestEsRender:
    @staticmethod
    def test_none_returns_none():
        assert _es()._render_filters(None) is None

    @staticmethod
    def test_single_eq():
        body = _es()._render_filters(FilterGroup(conditions=[
            FilterCondition(field="category", op=FilterOperator.EQ, value="work"),
        ]))
        # Single-condition AND group renders as bool.filter with one clause
        # (keeps the AND semantics consistent for downstream ES optimizations).
        assert body == {"bool": {"filter": [{"term": {"category": "work"}}]}}

    @staticmethod
    def test_single_ne():
        body = _es()._render_filters(FilterGroup(conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
        ]))
        # NE renders as bool.must_not inside the AND group's bool.filter wrapper.
        assert body == {"bool": {"filter": [{"bool": {"must_not": [{"term": {"blacklisted": True}}]}}]}}

    @staticmethod
    def test_and_group_uses_bool_filter():
        g = FilterGroup(logic=FilterLogic.AND, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
            FilterCondition(field="b", op=FilterOperator.NE, value=2),
        ])
        body = _es()._render_filters(g)
        assert body == {"bool": {"filter": [
            {"term": {"a": 1}},
            {"bool": {"must_not": [{"term": {"b": 2}}]}},
        ]}}

    @staticmethod
    def test_or_group_uses_bool_should():
        g = FilterGroup(logic=FilterLogic.OR, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value="x"),
            FilterCondition(field="a", op=FilterOperator.EQ, value="y"),
        ])
        body = _es()._render_filters(g)
        assert body == {"bool": {
            "should": [{"term": {"a": "x"}}, {"term": {"a": "y"}}],
            "minimum_should_match": 1,
        }}


# ---------------------------------------------------------------------------
# Gauss (PostgreSQL-style) render
# ---------------------------------------------------------------------------

def _gauss():
    from jiuwen_memory.foundation.store.vector.gauss_vector_store import GaussVectorStore
    store = GaussVectorStore.__new__(GaussVectorStore)
    return store


class TestGaussRender:
    @staticmethod
    def test_none_returns_none():
        assert _gauss()._render_filters(None) is None

    @staticmethod
    def test_single_eq_string_escaping():
        expr = _gauss()._render_filters(FilterGroup(conditions=[
            FilterCondition(field="name", op=FilterOperator.EQ, value="O'Brien"),
        ]))
        # Single quotes must be escaped to prevent SQL injection.
        assert expr == "name = 'O''Brien'"

    @staticmethod
    def test_single_eq_bool():
        expr = _gauss()._render_filters(FilterGroup(conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.NE, value=False),
        ]))
        assert expr == "blacklisted != FALSE"

    @staticmethod
    def test_single_eq_int():
        expr = _gauss()._render_filters(FilterGroup(conditions=[
            FilterCondition(field="count", op=FilterOperator.EQ, value=10),
        ]))
        assert expr == "count = 10"

    @staticmethod
    def test_single_eq_null():
        expr = _gauss()._render_filters(FilterGroup(conditions=[
            FilterCondition(field="x", op=FilterOperator.EQ, value=None),
        ]))
        assert expr == "x = NULL"

    @staticmethod
    def test_and_group():
        g = FilterGroup(logic=FilterLogic.AND, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
            FilterCondition(field="b", op=FilterOperator.NE, value="x"),
        ])
        assert _gauss()._render_filters(g) == "a = 1 AND b != 'x'"

    @staticmethod
    def test_or_group():
        g = FilterGroup(logic=FilterLogic.OR, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
            FilterCondition(field="a", op=FilterOperator.EQ, value=2),
        ])
        assert _gauss()._render_filters(g) == "a = 1 OR a = 2"

    @staticmethod
    def test_nested_group_renders_parentheses():
        inner = FilterGroup(logic=FilterLogic.OR, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
            FilterCondition(field="a", op=FilterOperator.EQ, value=2),
        ])
        outer = FilterGroup(conditions=[
            inner,
            FilterCondition(field="b", op=FilterOperator.NE, value=3),
        ])
        assert _gauss()._render_filters(outer) == "(a = 1 OR a = 2) AND b != 3"
