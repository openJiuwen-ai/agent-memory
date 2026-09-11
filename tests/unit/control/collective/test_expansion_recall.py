"""多空间先选根、后展开；父子预算不按空间重置，也不丢失证据组。"""

import asyncio
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.type_def import FilterClause, FilterOp
from jiuwen_memory.control.collective.cross_space_recall import SpaceRecallTarget, recall_spaces
from jiuwen_memory.retrieval.expander_impl.default_expander import DefaultExpander
from jiuwen_memory.retrieval.expansion import disclosed_tokens
from jiuwen_memory.retrieval.types import RetrievalResult, RetrievedItem
from tests.unit.retrieval.expansion_fixtures import make_space_tree
from tests.unit.retrieval.hierarchy_query_fixtures import hierarchy_query

pytestmark = pytest.mark.unit


def test_cross_space_top_k_selects_roots_without_truncating_their_children() -> None:
    trees = {name: make_space_tree(name) for name in ("one", "two")}
    targets = [SpaceRecallTarget(tree.root.scope) for tree in trees.values()]

    async def recall(scope, query):
        return trees[scope.space].base.retriever.retrieve(scope, query)

    result, failures = asyncio.run(recall_spaces(
        targets, hierarchy_query(expand_depth=1), top_k=2, recall=recall,
    ))
    assert len(result.items) == 6
    assert [item.unit_id for item in result.items[:2]] == ["root", "root"]
    assert failures == [] and result.errors == []


def test_unselected_root_never_reads_children() -> None:
    trees = {name: make_space_tree(name) for name in ("one", "two")}
    stores = {}
    for name, tree in trees.items():
        stores[name] = Mock(wraps=tree.base.domain)
        tree.base.retriever.bind_expander(DefaultExpander(stores[name]))

    async def recall(scope, query):
        return trees[scope.space].base.retriever.retrieve(scope, query)

    targets = [SpaceRecallTarget(tree.root.scope) for tree in trees.values()]
    result, failures = asyncio.run(recall_spaces(
        targets, hierarchy_query(expand_depth=1), top_k=1, recall=recall,
    ))
    assert len(result.items) == 3
    assert "one" in result.items[0].content
    assert failures == []
    stores["two"].get.assert_not_called()


def test_cross_space_budget_is_global_and_diagnostics_survive() -> None:
    trees = {name: make_space_tree(name) for name in ("one", "two")}

    async def recall(scope, query):
        return trees[scope.space].base.retriever.retrieve(scope, query)

    targets = [SpaceRecallTarget(tree.root.scope) for tree in trees.values()]
    result, failures = asyncio.run(recall_spaces(
        targets, hierarchy_query(expand_depth=1, max_tokens=4, with_trajectory=True),
        top_k=2, recall=recall,
    ))
    assert sum(disclosed_tokens(item) for item in result.items) <= 4
    assert len(result.items) == 3
    assert failures == []
    assert any(error.error_type == "budget_exhausted" for error in result.errors)
    assert len([step for step in result.trajectory if step.stage == "expand"]) == 2


def test_each_space_keeps_its_own_child_visibility_predicate() -> None:
    trees = {name: make_space_tree(name) for name in ("one", "two")}
    for name, tree in trees.items():
        tree.root.user_metadata["visible"] = name
        tree.children[0].user_metadata["visible"] = name
        tree.children[1].user_metadata["visible"] = "denied"
        tree.save([tree.root, *tree.children])
        tree.base.keyword_builder.update([tree.root])
        tree.base.vector_builder.update([tree.root])

    async def recall(scope, query):
        return trees[scope.space].base.retriever.retrieve(scope, query)

    targets = [SpaceRecallTarget(
        tree.root.scope, (FilterClause("user_metadata.visible", FilterOp.EQ, name),),
    ) for name, tree in trees.items()]
    result, failures = asyncio.run(recall_spaces(
        targets, hierarchy_query(expand_depth=1), top_k=2, recall=recall,
    ))
    assert len(result.items) == 4
    assert [item.unit_id for item in result.items[2:]] == ["first", "first"]
    assert failures == []
    assert all(item.user_metadata["visible"] != "denied" for item in result.items)


def test_unsupported_deferred_provider_is_a_space_failure_not_false_success() -> None:
    tree = make_space_tree("one")

    async def incompatible_recall(scope, query):
        return RetrievalResult(items=[RetrievedItem(unit_id="root", content="no preparation")])

    result, failures = asyncio.run(recall_spaces(
        [SpaceRecallTarget(tree.root.scope)], hierarchy_query(expand_depth=1),
        top_k=1, recall=incompatible_recall,
    ))
    assert result.items == []
    assert len(failures) == 1
    assert failures[0].error_type == "UnsupportedCapabilityError"
