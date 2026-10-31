"""跨空间先选父，再按空间与融合原序补位，共享预算并复核各空间权限。"""

import asyncio
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.type_def import FilterClause, FilterOp
from jiuwen_memory.control.collective.cross_space_recall import SpaceRecallTarget, recall_spaces
from tests.unit.retrieval.hierarchy_query_fixtures import ConstantEmbedder, hierarchy_query
from tests.unit.retrieval.rollup_fixtures import make_rollup_space

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("limit,budget,merge_roots", [
    (4, None, False), (5, None, False), (6, None, False),
    (6, 4, False), (6, None, True),
])
def test_cross_space_children_keep_space_and_fusion_order(limit, budget, merge_roots, monkeypatch):
    trees = {name: make_rollup_space(name) for name in ("two", "one")}
    if merge_roots:
        trees["one"].root.segments = list(trees["two"].root.segments)
        trees["one"].save([trees["one"].root])
    forbid = Mock(side_effect=AssertionError("must reuse recalled children"))
    for tree in trees.values():
        monkeypatch.setattr(tree.expander, "expand", forbid)
    monkeypatch.setattr(ConstantEmbedder, "embed", forbid)

    async def recall(scope, query):
        if scope.space == "two":
            await asyncio.sleep(0)  # 故意先完成后一个空间，结果仍须遵循目标顺序。
        return trees[scope.space].base.retriever.retrieve(scope, query)

    result, failures = asyncio.run(recall_spaces(
        [SpaceRecallTarget(tree.root.scope) for tree in trees.values()],
        hierarchy_query(rollup=True, expand_depth=1, top_k=limit, max_tokens=budget),
        top_k=limit, recall=recall,
    ))
    selected_spaces = ["two"] if merge_roots else ["two", "one"]
    expected = [(name, "parent") for name in selected_spaces] + [
        (name, f"leaf-{index}") for name in selected_spaces for index in range(2)
    ]
    assert [(item.user_metadata["allowed"], item.unit_id) for item in result.items] == (
        expected[:min(limit, budget or limit)]
    )
    assert failures == []
    assert [error.error_type for error in result.errors] == (
        ["budget_exhausted"] if budget is not None else []
    )
    forbid.assert_not_called()


@pytest.mark.parametrize("depth", [0, 1])
def test_each_space_rechecks_its_own_parent_permission_filters(depth) -> None:
    trees = {name: make_rollup_space(name) for name in ("one", "two")}
    trees["two"].root.user_metadata["allowed"] = "denied"
    trees["two"].save([trees["two"].root])

    async def recall(scope, query):
        return trees[scope.space].base.retriever.retrieve(scope, query)

    targets = [SpaceRecallTarget(
        tree.root.scope, (FilterClause("user_metadata.allowed", FilterOp.EQ, name),),
    ) for name, tree in trees.items()]
    result, failures = asyncio.run(recall_spaces(
        targets, hierarchy_query(rollup=True, expand_depth=depth, top_k=4),
        top_k=4, recall=recall,
    ))
    assert len(result.items) == 1 + 2 * depth
    assert all(item.user_metadata["allowed"] == "one" for item in result.items)
    assert failures == []
    assert any(error.error_type == "visibility_excluded" for error in result.errors)
