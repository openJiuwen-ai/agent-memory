"""InMemoryFusionStore 的标量过滤：AND/OR/NOT 树形谓词求值。"""

from __future__ import annotations

import pytest

from common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from common.type_def import FilterClause, FilterGroup, FilterLogic, FilterOp, Scope
from storage.fusion_impl.in_memory_fusion_store import InMemoryFusionStore
from storage.types import FusionQuery, FusionRecord

pytestmark = pytest.mark.unit

SCOPE = Scope(org="o", user="u", agent="a", session="s")


def _store() -> InMemoryFusionStore:
    store = InMemoryFusionStore(WhitespaceTokenizer())
    store.insert(
        SCOPE,
        [
            FusionRecord(id="r1", vector=[1.0, 0.0], scalars={"project": "alpha", "priority": 8}),
            FusionRecord(id="r2", vector=[1.0, 0.0], scalars={"project": "beta", "priority": 3}),
            FusionRecord(id="r3", vector=[1.0, 0.0], scalars={"project": "gamma", "priority": 9}),
        ],
    )
    return store


def _search_ids(store: InMemoryFusionStore, filters) -> set[str]:
    # 纯向量打分（vector_weight=1.0），三行 cosine 均为 1 → 命中与否只由 scalar_filters 决定
    q = FusionQuery(vector=[1.0, 0.0], top_k=10, vector_weight=1.0, scalar_filters=filters)
    return {hit.id for hit in store.search(SCOPE, q)}


def test_fusion_and_or_combination() -> None:
    store = _store()
    # (project ∈ {alpha, beta}) AND priority >= 5
    expr = FilterGroup(
        FilterLogic.AND,
        [
            FilterGroup(
                FilterLogic.OR,
                [
                    FilterClause("project", FilterOp.EQ, "alpha"),
                    FilterClause("project", FilterOp.EQ, "beta"),
                ],
            ),
            FilterClause("priority", FilterOp.GTE, 5),
        ],
    )

    assert _search_ids(store, expr) == {"r1"}, "beta 被 priority 拦下，gamma 被 OR 拦下"


def test_fusion_not_excludes() -> None:
    store = _store()
    expr = FilterGroup(FilterLogic.NOT, [FilterClause("project", FilterOp.EQ, "beta")])

    assert _search_ids(store, expr) == {"r1", "r3"}, "NOT 排除 beta"


def test_fusion_none_filter_returns_all() -> None:
    store = _store()

    assert _search_ids(store, None) == {"r1", "r2", "r3"}, "无过滤 → 全命中"
