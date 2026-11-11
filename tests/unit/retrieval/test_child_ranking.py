"""复用真实召回候选：父优先、融合原序、深度与预算；不再向量化子正文。"""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import UnsupportedCapabilityError
from jiuwen_memory.common.type_def import (
    HierarchyRole,
    RecallChannel,
    RetrievalPipeline,
    Segment,
)
from jiuwen_memory.retrieval.expander import MAX_EXPANSION_NODES
from jiuwen_memory.retrieval.expander_impl.default_expander import DefaultExpander
from jiuwen_memory.retrieval.expansion import PreparedRetrievalResult, complete_expansion
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.types import DisclosureLevel
from tests.unit.retrieval.expansion_fixtures import link, make_tree
from tests.unit.retrieval.hierarchy_query_fixtures import (
    QUERY_SCOPE,
    ConstantEmbedder,
    hierarchy_query,
    make_harness,
    tree_unit,
)
from tests.unit.retrieval.rollup_fixtures import make_rollup_space

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
def test_recalled_children_keep_fusion_order_without_expansion_or_embedding(pipeline, monkeypatch):
    tree = make_tree(pipeline, index_children=True)
    hidden = tree_unit("not-recalled", HierarchyRole.SNAPSHOT)
    link(tree.root, [*tree.children, hidden])
    tree.save([tree.root])
    tree.base.domain.add(QUERY_SCOPE, [hidden])
    other_parent = tree_unit("unselected-parent")
    other_child = tree_unit("unselected-child", HierarchyRole.SNAPSHOT)
    link(other_parent, [other_child])
    tree.base.add([other_parent, other_child])

    def rerank_scores(_query: str, texts: list[str]) -> list[float]:
        scores = []
        for text in texts:
            score = 0.9 if text.endswith("1") else 0.1
            scores.append(score)
        return scores

    reranker = Mock()
    reranker.rerank.side_effect = rerank_scores
    retriever = PipelineRetriever(
        tree.base.parser, RRFFuser(), tree.base.discloser, None, reranker,
        domain_store=tree.base.domain, min_score=0.5,
    )
    retriever.bind_expander(tree.expander)
    forbid = Mock(side_effect=AssertionError("child selection must reuse recalled units"))
    monkeypatch.setattr(tree.expander, "expand", forbid)
    monkeypatch.setattr(ConstantEmbedder, "embed", forbid)
    query = hierarchy_query(rollup=True, expand_depth=1, top_k=3, with_trajectory=True)
    result = retriever.retrieve(QUERY_SCOPE, query)
    assert [item.unit_id for item in result.items] == ["root", "first", "second"]
    assert all(item.score == 0.9 for item in result.items)
    assert not result.errors
    assert result.trajectory[-1].stage == "child_select"
    assert result.trajectory[-1].candidate_count == 2
    steps = [step for step in result.trajectory if step.stage == "expand"]
    assert len(steps) == 1
    assert steps[0].detail.items() >= {
        "root_id": "root", "mode": "recalled_children", "requested_depth": "1",
        "actual_depth": "1", "item_count": "2", "truncated": "false", "complete": "true",
    }.items()
    for limit in (1, 2):
        limited = retriever.retrieve(QUERY_SCOPE, replace(query, top_k=limit))
        assert limited.items == result.items[:limit]
        assert not limited.errors
        steps = [step for step in limited.trajectory if step.stage == "expand"]
        assert len(steps) == 1
        assert steps[0].detail["item_count"] == str(limit - 1)
        assert steps[0].detail["actual_depth"] == str(limit - 1)
        assert steps[0].detail["stop_reason"] == ("top_k_full" if limit == 1 else "top_k_limit")
        assert steps[0].detail["truncated"] == str(limit == 2).lower()
        assert steps[0].detail["complete"] == "false"
    limited = retriever.retrieve(QUERY_SCOPE, replace(query, with_trajectory=False))
    assert limited.items == result.items and limited.errors == result.errors
    assert limited.trajectory == []
    forbid.assert_not_called()


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
def test_role_filter_precedes_recall_limit_and_rechecks_stale_index(pipeline):
    harness = make_harness(pipeline, recall_limit=1)
    parent = tree_unit("parent")
    leaf = tree_unit("leaf", HierarchyRole.SNAPSHOT)
    link(parent, [leaf])
    harness.domain.add(QUERY_SCOPE, [parent])
    harness.add([tree_unit("scene", HierarchyRole.SCENE),
                 tree_unit("event", HierarchyRole.EVENT), leaf])
    query = hierarchy_query(rollup=True, top_k=1, channels=[RecallChannel.VECTOR])
    result = harness.retriever.retrieve(QUERY_SCOPE, query)
    assert [item.unit_id for item in result.items] == [parent.id]
    leaf.hierarchy.role = HierarchyRole.SCENE
    harness.domain.update(QUERY_SCOPE, [leaf])
    assert harness.retriever.retrieve(QUERY_SCOPE, query).items == []


@pytest.mark.parametrize("role,depth", [
    (HierarchyRole.SCENE, 1), (HierarchyRole.SCENE, 2),
    (HierarchyRole.EVENT, 1), (HierarchyRole.EVENT, 2), (HierarchyRole.EVENT, 3),
])
def test_recalled_descendants_respect_actual_ancestor_depth(role, depth):
    tree = make_tree(index_children=True)
    roles = (
        HierarchyRole.SNAPSHOT, HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.EVENT,
    )
    chain = [tree_unit(value.value, value) for value in roles]
    for parent, child in zip(chain[1:], chain):
        link(parent, [child])
    tree.base.add(chain)
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        hierarchy_role=role, rollup=True, expand_depth=depth, with_trajectory=True,
        channels=[RecallChannel.KEYWORD],
    ))
    index = next(i for i, unit in enumerate(chain) if unit.hierarchy.role is role)
    assert result.items[0].unit_id == chain[index].id
    assert {item.unit_id for item in result.items} == {
        unit.id for unit in chain[max(0, index - depth):index + 1]
    }
    assert not result.errors
    step = next(step for step in result.trajectory if step.stage == "expand")
    assert step.detail["root_id"] == chain[index].id
    assert step.detail["actual_depth"] == str(depth)
    assert step.detail["item_count"] == str(depth)


def test_empty_recalled_children_are_prepared_without_errors():
    tree = make_tree()
    query = hierarchy_query(
        rollup=True, expand_depth=1, top_k=3, with_trajectory=True, defer_expansion=True,
    )
    prepared = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    assert prepared.recalled_children_ready
    assert prepared.recalled_children == []
    result = complete_expansion(prepared, [prepared], query)
    assert [item.unit_id for item in result.items] == ["root"]
    assert not result.errors
    assert result.trajectory[-1].stage == "child_select"
    assert result.trajectory[-1].candidate_count == 0
    assert result.trajectory[-1].detail["selected_children"] == "0"
    step = next(step for step in result.trajectory if step.stage == "expand")
    assert step.detail["actual_depth"] == step.detail["item_count"] == "0"
    assert step.detail["complete"] == "true"
    assert step.detail["truncated"] == "false"


@pytest.mark.parametrize("top_k", [1, 3])
def test_unprepared_selected_provider_is_rejected_only_with_spare_window(top_k):
    tree = make_tree(index_children=True)
    query = hierarchy_query(
        rollup=True, expand_depth=1, top_k=top_k, defer_expansion=True, with_trajectory=True,
    )
    prepared = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    provider = PreparedRetrievalResult(
        items=prepared.items, expansion_roots=prepared.expansion_roots,
    )
    if top_k == 1:
        result = complete_expansion(provider, [provider], query)
        assert [item.unit_id for item in result.items] == ["root"]
        assert not result.errors
        assert result.trajectory[-1].detail["stop_reason"] == "top_k_full"
    else:
        with pytest.raises(UnsupportedCapabilityError, match="未完成已召回后代的准备"):
            complete_expansion(provider, [provider], query)


def test_unselected_provider_does_not_need_child_preparation():
    selected_tree = make_rollup_space("selected")
    other_tree = make_rollup_space("other")
    query = hierarchy_query(rollup=True, expand_depth=1, top_k=3, defer_expansion=True)
    selected = selected_tree.base.retriever.retrieve(selected_tree.root.scope, query)
    other = other_tree.base.retriever.retrieve(other_tree.root.scope, query)
    provider = PreparedRetrievalResult(items=other.items, expansion_roots=other.expansion_roots)
    result = complete_expansion(selected, [provider, selected], query)
    assert [item.unit_id for item in result.items] == ["parent", "leaf-0", "leaf-1"]
    assert not result.errors


def test_recalled_child_with_broken_reverse_edge_is_excluded():
    tree = make_tree(index_children=True)
    tree.root.hierarchy.child_ids.pop()
    tree.root.hierarchy.child_scopes.pop()
    tree.save([tree.root])
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(rollup=True, expand_depth=1))
    assert [item.unit_id for item in result.items] == ["root", "first"]
    assert any(error.error_type == "child_mismatch" for error in result.errors)


@pytest.mark.parametrize("rollup", [False, True])
def test_blank_children_consume_budget_before_exclusion(rollup):
    tree = make_tree(index_children=True)
    tree.children[0].segments = [Segment(content="   ")]
    tree.children[0].layers.l0 = ""
    tree.save(tree.children)
    query = hierarchy_query(
        rollup=rollup, expand_depth=1, disclosure=DisclosureLevel.L0, with_trajectory=True,
    )
    result = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    assert [item.unit_id for item in result.items] == ["root", "second"]
    if rollup:
        step = next(step for step in result.trajectory if step.stage == "expand")
        assert step.detail["item_count"] == "1"
        assert step.detail["visited_count"] == "2"
    limited = tree.base.retriever.retrieve(QUERY_SCOPE, replace(query, max_tokens=2))
    assert [item.unit_id for item in limited.items] == ["root"]
    assert any(error.error_type == "budget_exhausted" for error in limited.errors)


def test_l0_disclosure_checks_source_body_and_keeps_recalled_children(monkeypatch):
    tree = make_tree(index_children=True)
    original = tree.base.discloser.disclose

    def disclose(*args, **kwargs):
        return [replace(item, content="") for item in original(*args, **kwargs)]

    monkeypatch.setattr(tree.base.discloser, "disclose", disclose)
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(rollup=True, expand_depth=1))
    assert [item.unit_id for item in result.items] == ["root", "first", "second"]


def test_structural_expansion_preserves_scope_identity_and_bfs_order():
    tree = make_tree()
    tree.children[1].id = tree.children[0].id
    link(tree.root, tree.children)
    tree.save([tree.root])
    tree.base.domain.add(tree.children[1].scope, [tree.children[1]])
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(expand_depth=1))
    assert [item.unit_id for item in result.items] == ["root", "first", "first"]
    assert [item.abstract for item in result.items[1:]] == ["leaf-0", "leaf-1"]


def test_blank_structural_root_still_expands_nonempty_children():
    tree = make_tree()
    query = hierarchy_query(top_k=1, expand_depth=1, defer_expansion=True)
    prepared = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    prepared.expansion_roots[0].candidate.unit.segments = [Segment(content="  ")]
    result = complete_expansion(prepared, [prepared], query)
    assert [item.unit_id for item in result.items] == ["first"]


@pytest.mark.parametrize("stop_reason", ["budget_exhausted", "top_k_limit", ""])
def test_recalled_trace_is_scoped_and_retains_partial_progress(stop_reason):
    """跨空间同名根独立计数，停止不丢失已返回证据或重复报错。"""
    first, second = make_rollup_space("first"), make_rollup_space("second")
    query = hierarchy_query(rollup=True, expand_depth=1, defer_expansion=True, with_trajectory=True)
    prepared = [
        tree.base.retriever.retrieve(tree.root.scope, query) for tree in (first, second)
    ]
    selected = replace(prepared[0], items=prepared[0].items + prepared[1].items)
    # 第一根的首个子成功，第二个子因预算或窗口停止；第二根只记录跳过原因。
    if stop_reason == "budget_exhausted":
        query = replace(query, max_tokens=3)
        prepared[0].recalled_children.append(prepared[0].recalled_children[0])
    elif stop_reason == "top_k_limit":
        query = replace(query, top_k=3)
    result = complete_expansion(selected, prepared, query)
    steps = [step for step in result.trajectory if step.stage == "expand"]
    assert len(steps) == 2
    assert [json.loads(step.detail["root_scope"])["space"] for step in steps] == ["first", "second"]
    assert all(step.detail["root_id"] == "parent" for step in steps)
    expected_counts = ["1", "0"] if stop_reason else ["2", "2"]
    expected_depths = ["1", "0"] if stop_reason else ["1", "1"]
    assert [step.detail["item_count"] for step in steps] == expected_counts
    assert [step.detail["actual_depth"] for step in steps] == expected_depths
    assert all(step.detail["stop_reason"] == stop_reason for step in steps)
    assert all(step.detail["truncated"] == str(bool(stop_reason)).lower() for step in steps)
    assert steps[0].detail["visited_count"] == ("1" if stop_reason == "top_k_limit" else "2")
    assert [error.error_type for error in result.errors] == (
        ["budget_exhausted"] if stop_reason == "budget_exhausted" else []
    )
    plain = complete_expansion(selected, prepared, replace(query, with_trajectory=False))
    assert plain.items == result.items and plain.errors == result.errors


def test_recalled_node_limit_preserves_selected_count():
    """全局节点上限截断时，轨迹保留已完成根和部分完成根的计数。"""
    # 单空间上卷本身也有 1000 节点上限；两个提供方才能触达共享补位上限。
    size = MAX_EXPANSION_NODES // 2 + 1
    query = hierarchy_query(
        rollup=True, expand_depth=1, top_k=2 * size + 2,
        with_trajectory=True, defer_expansion=True,
    )
    prepared = []
    for group in range(2):
        base = make_harness(RetrievalPipeline.RECALL_GET_RANK, recall_limit=size + 1)
        parent = tree_unit(f"parent-{group}")
        children = [
            tree_unit(f"child-{group}-{index}", HierarchyRole.SNAPSHOT) for index in range(size)
        ]
        link(parent, children)
        base.add([parent, *children])
        base.retriever.bind_expander(DefaultExpander(base.domain))
        prepared.append(base.retriever.retrieve(QUERY_SCOPE, query))
    selected = replace(prepared[0], items=prepared[0].items + prepared[1].items)
    result = complete_expansion(selected, prepared, query)
    steps = [step for step in result.trajectory if step.stage == "expand"]
    assert len(result.items) == MAX_EXPANSION_NODES + 2
    assert sum(int(step.detail["item_count"]) for step in steps) == MAX_EXPANSION_NODES
    assert sum(int(step.detail["visited_count"]) for step in steps) == MAX_EXPANSION_NODES
    assert steps[0].detail["complete"] == "true"
    assert steps[1].detail["actual_depth"] == "1"
    assert steps[1].detail["stop_reason"] == "node_limit"
    assert steps[1].detail["truncated"] == "true"
    assert [error.error_type for error in result.errors] == ["node_limit"]
