"""空间内先上卷，空间间选根后才按共享预算展开。"""

import asyncio
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.type_def import FilterClause, FilterOp
from jiuwen_memory.control.collective.cross_space_recall import SpaceRecallTarget, recall_spaces
from jiuwen_memory.retrieval.expander_impl.default_expander import DefaultExpander
from jiuwen_memory.retrieval.expansion import disclosed_tokens
from tests.unit.retrieval.hierarchy_query_fixtures import hierarchy_query
from tests.unit.retrieval.rollup_fixtures import make_rollup_space

pytestmark = pytest.mark.unit


def test_promoted_roots_share_global_disclosure_budget() -> None:
    trees = {name: make_rollup_space(name) for name in ("one", "two")}

    async def recall(scope, query):
        return trees[scope.space].base.retriever.retrieve(scope, query)

    targets = [SpaceRecallTarget(tree.root.scope) for tree in trees.values()]
    result, failures = asyncio.run(recall_spaces(
        targets, hierarchy_query(rollup=True, expand_depth=1, max_tokens=4),
        top_k=2, recall=recall,
    ))
    assert len(result.items) == 4
    assert sum(disclosed_tokens(item) for item in result.items) == 4
    assert "one" in result.items[0].content
    assert "two" in result.items[1].content
    assert failures == []
    assert any(error.error_type == "budget_exhausted" for error in result.errors)


def test_unselected_promoted_parent_does_not_expand() -> None:
    trees = {name: make_rollup_space(name) for name in ("one", "two")}
    unselected_store = Mock(wraps=trees["two"].base.domain)
    trees["two"].base.retriever.bind_expander(DefaultExpander(unselected_store))

    async def recall(scope, query):
        return trees[scope.space].base.retriever.retrieve(scope, query)

    result, failures = asyncio.run(recall_spaces(
        [SpaceRecallTarget(tree.root.scope) for tree in trees.values()],
        hierarchy_query(rollup=True, expand_depth=1), top_k=1, recall=recall,
    ))
    assert len(result.items) == 3
    assert failures == []
    assert "one" in result.items[0].content
    unselected_store.get.assert_not_called()


def test_each_space_rechecks_its_own_parent_permission_filters() -> None:
    trees = {name: make_rollup_space(name) for name in ("one", "two")}
    trees["two"].root.user_metadata["allowed"] = "denied"
    trees["two"].save([trees["two"].root])

    async def recall(scope, query):
        return trees[scope.space].base.retriever.retrieve(scope, query)

    targets = [SpaceRecallTarget(
        tree.root.scope, (FilterClause("user_metadata.allowed", FilterOp.EQ, name),),
    ) for name, tree in trees.items()]
    result, failures = asyncio.run(recall_spaces(
        targets, hierarchy_query(rollup=True), top_k=2, recall=recall,
    ))
    assert len(result.items) == 1
    assert result.items[0].user_metadata["allowed"] == "one"
    assert failures == []
    assert any(error.error_type == "visibility_excluded" for error in result.errors)
