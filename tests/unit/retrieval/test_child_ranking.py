"""复用真实召回候选：父优先、融合原序、深度与预算；不再向量化子正文。"""

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
    for limit in (1, 2):
        limited = retriever.retrieve(QUERY_SCOPE, replace(query, top_k=limit))
        assert limited.items == result.items[:limit]
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
        hierarchy_role=role, rollup=True, expand_depth=depth,
        channels=[RecallChannel.KEYWORD],
    ))
    index = next(i for i, unit in enumerate(chain) if unit.hierarchy.role is role)
    assert result.items[0].unit_id == chain[index].id
    assert {item.unit_id for item in result.items} == {
        unit.id for unit in chain[max(0, index - depth):index + 1]
    }
    assert not result.errors


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


@pytest.mark.parametrize("top_k", [1, 3])
def test_unprepared_selected_provider_is_rejected_only_with_spare_window(top_k):
    tree = make_tree(index_children=True)
    query = hierarchy_query(rollup=True, expand_depth=1, top_k=top_k, defer_expansion=True)
    prepared = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    provider = PreparedRetrievalResult(
        items=prepared.items, expansion_roots=prepared.expansion_roots,
    )
    if top_k == 1:
        result = complete_expansion(provider, [provider], query)
        assert [item.unit_id for item in result.items] == ["root"]
        assert not result.errors
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
    query = hierarchy_query(rollup=rollup, expand_depth=1, disclosure=DisclosureLevel.L0)
    result = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    assert [item.unit_id for item in result.items] == ["root", "second"]
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
