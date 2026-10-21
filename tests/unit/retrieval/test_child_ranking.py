"""父优先和子相关性排序使用真实展开链路，不更改候选预算或父分数。"""

import asyncio
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import BackendError, UnsupportedCapabilityError
from jiuwen_memory.common.type_def import HierarchyRole, RecallChannel, RetrievalPipeline, Segment
from jiuwen_memory.control.collective.cross_space_recall import SpaceRecallTarget, recall_spaces
from jiuwen_memory.retrieval.child_ranking import child_cosine_order
from jiuwen_memory.retrieval.types import DisclosureLevel
from tests.unit.retrieval.expansion_fixtures import link, make_space_tree, make_tree
from tests.unit.retrieval.hierarchy_query_fixtures import (
    QUERY_SCOPE,
    ConstantEmbedder,
    hierarchy_query,
    tree_unit,
)

pytestmark = pytest.mark.unit


class RecordingEmbedder(ConstantEmbedder):
    def __init__(self, vectors=None):
        self.calls = []
        self.vectors = vectors or {}

    def embed(self, texts):
        self.calls.append(list(texts))
        return [self.vectors.get(text, [1.0, 0.0]) for text in texts]


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
def test_children_rank_by_raw_question_and_full_body_without_changing_scores(pipeline, monkeypatch):
    tree = make_tree(pipeline)
    first, second = tree.children
    embedder = RecordingEmbedder({first.content: [0.0, 1.0], second.content: [1.0, 0.0]})
    tree.base.retriever.bind_embedder(embedder)
    original = tree.base.parser.parse

    def parse(request):
        result = original(request)
        result.raw = "rewritten query differs"
        return result

    monkeypatch.setattr(tree.base.parser, "parse", parse)
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        top_k=3, expand_depth=1, channels=[RecallChannel.KEYWORD],
        disclosure=DisclosureLevel.L0, with_trajectory=True,
    ))
    assert [item.unit_id for item in result.items] == ["root", "second", "first"]
    assert embedder.calls == [["recall evidence", first.content, second.content]]
    assert all(item.score == result.items[0].score for item in result.items)
    assert all(item.level is DisclosureLevel.L0 for item in result.items)
    assert result.trajectory[-1].stage == "child_rank"


@pytest.mark.parametrize("top_k", [1, 2])
def test_enough_parents_fill_output_without_child_embedding(top_k, monkeypatch):
    tree = make_tree()
    if top_k == 2:
        second_root = tree_unit("another-root")
        tree.base.add([second_root])
    embedder = RecordingEmbedder()
    tree.base.retriever.bind_embedder(embedder)
    query = hierarchy_query(top_k=top_k, expand_depth=1)
    roots = tree.base.retriever.retrieve(QUERY_SCOPE, replace(query, expand_depth=0))
    result = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    assert [item.unit_id for item in result.items[:top_k]] == [
        item.unit_id for item in roots.items
    ]
    assert len(result.items) == top_k
    assert embedder.calls == []


def test_no_children_or_snapshot_search_never_embeds(monkeypatch):
    tree = make_tree()
    embedder = RecordingEmbedder()
    tree.base.retriever.bind_embedder(embedder)
    tree.root.hierarchy.child_ids = []
    tree.root.hierarchy.child_scopes = []
    tree.save([tree.root])
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(expand_depth=1))
    assert [item.unit_id for item in result.items] == ["root"]
    tree.root.hierarchy.role = HierarchyRole.SNAPSHOT
    tree.save([tree.root])
    tree.base.keyword_builder.update([tree.root])
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        expand_depth=1, hierarchy_role=HierarchyRole.SNAPSHOT,
    ))
    assert [item.unit_id for item in result.items] == ["root"]
    assert embedder.calls == []


def test_scope_identity_dedup_and_equal_similarity_keep_original_order(monkeypatch):
    tree = make_tree()
    tree.children[1].id = tree.children[0].id
    link(tree.root, tree.children)
    tree.save([tree.root])
    tree.base.domain.add(tree.children[1].scope, [tree.children[1]])
    embedder = RecordingEmbedder()
    tree.base.retriever.bind_embedder(embedder)
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(expand_depth=1))
    assert [item.unit_id for item in result.items] == ["root", "first", "first"]
    assert [item.abstract for item in result.items[1:]] == ["leaf-0", "leaf-1"]
    assert len(embedder.calls[0]) == 3


@pytest.mark.parametrize("top_k", [2, 3, 5, 6])
def test_cross_space_children_share_one_global_pool_before_total_cutoff(monkeypatch, top_k):
    trees = {name: make_space_tree(name) for name in ("one", "two")}
    embedder = RecordingEmbedder({"one-low": [0.0, 1.0], "two-low": [-1.0, 0.0]})
    for name, tree in trees.items():
        for index, child in enumerate(tree.children):
            child.segments = [Segment(content=f"{name}-{'low' if index == 0 else 'high'}")]
        tree.save(tree.children)
        tree.base.retriever.bind_embedder(embedder)

    async def recall(scope, query):
        return trees[scope.space].base.retriever.retrieve(scope, query)

    result, failures = asyncio.run(recall_spaces(
        [SpaceRecallTarget(tree.root.scope) for tree in trees.values()],
        hierarchy_query(top_k=top_k, expand_depth=1), top_k=top_k, recall=recall,
    ))
    assert failures == []
    assert [item.content for item in result.items] == [
        "recall evidence one", "recall evidence two", "one-high", "two-high", "one-low", "two-low",
    ][:top_k]
    assert len(embedder.calls) == int(top_k > 2)
    assert len(result.items) == min(top_k, 6), "top_k 限制跨空间父子结果总数"


def test_missing_embedder_is_not_a_silent_bfs_fallback(monkeypatch):
    tree = make_tree()
    tree.base.retriever.bind_embedder(None)
    with pytest.raises(UnsupportedCapabilityError, match="Embedder"):
        tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(top_k=3, expand_depth=1))


@pytest.mark.parametrize("vectors", [
    [], [[1.0, 0.0]], [[1.0, 0.0], [1.0]],
    [[1.0, 0.0], [float("nan"), 1.0]], [[1.0, 0.0], [float("inf"), 1.0]],
    [[1.0, 0.0], [0.0, 0.0]],
])
def test_invalid_embedding_is_rejected_instead_of_returning_unranked_children(vectors, monkeypatch):
    embedder = RecordingEmbedder()
    monkeypatch.setattr(embedder, "embed", lambda texts: vectors)
    with pytest.raises(BackendError, match="embedding"):
        child_cosine_order("question", ["child"], embedder)


def test_blank_child_is_removed_after_budget_and_never_embedded(monkeypatch):
    tree = make_tree()
    tree.children[0].segments = [Segment(content="   ")]
    tree.children[0].layers.l0 = ""
    tree.save(tree.children)
    embedder = RecordingEmbedder()
    tree.base.retriever.bind_embedder(embedder)
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(expand_depth=1))
    assert [item.unit_id for item in result.items] == ["root", "second"]
    assert embedder.calls == [["recall evidence", tree.children[1].content]]

    embedder.calls.clear()
    limited = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        expand_depth=1, max_tokens=2, disclosure=DisclosureLevel.L0,
    ))
    assert [item.unit_id for item in limited.items] == ["root"]
    assert any(error.error_type == "budget_exhausted" for error in limited.errors)
    assert embedder.calls == [], "空白正文已消耗的预算不能用于回填其他子节点"


def test_l0_disclosure_without_returned_content_keeps_nonempty_source(monkeypatch):
    tree = make_tree()
    embedder = RecordingEmbedder()
    tree.base.retriever.bind_embedder(embedder)
    original = tree.base.discloser.disclose

    def disclose(*args, **kwargs):
        return [replace(item, content="") for item in original(*args, **kwargs)]

    monkeypatch.setattr(tree.base.discloser, "disclose", disclose)
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(expand_depth=1))
    assert [item.unit_id for item in result.items] == ["root", "first", "second"]
    assert embedder.calls == [["recall evidence", *(child.content for child in tree.children)]]


def test_blank_parent_is_removed_but_its_nonempty_children_remain(monkeypatch):
    from jiuwen_memory.retrieval.expansion import complete_expansion

    tree = make_tree()
    embedder = RecordingEmbedder()
    tree.base.retriever.bind_embedder(embedder)
    query = hierarchy_query(top_k=1, expand_depth=1, defer_expansion=True)
    prepared = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    root = prepared.expansion_roots[0]
    root.candidate.unit.segments = [Segment(content="  ")]
    result = complete_expansion(prepared, [prepared], query)
    assert [item.unit_id for item in result.items] == ["first"]
    assert embedder.calls == [["recall evidence", *(child.content for child in tree.children)]]


def test_unspecified_parent_role_preserves_existing_empty_content_behavior(monkeypatch):
    tree = make_tree()
    tree.children[0].segments = [Segment(content="   ")]
    tree.save(tree.children)
    embedder = RecordingEmbedder()
    tree.base.retriever.bind_embedder(embedder)
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        hierarchy_role=None, expand_depth=1,
    ))
    assert [item.unit_id for item in result.items] == ["root", "first", "second"]
    assert embedder.calls == []


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
def test_total_cutoff_keeps_high_relevance_child_after_global_ranking(pipeline, monkeypatch):
    tree = make_tree(pipeline)
    first, second = tree.children
    embedder = RecordingEmbedder({first.content: [0.0, 1.0], second.content: [1.0, 0.0]})
    tree.base.retriever.bind_embedder(embedder)
    query = hierarchy_query(top_k=3, expand_depth=1, disclosure=DisclosureLevel.L2)
    full = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    limited = tree.base.retriever.retrieve(QUERY_SCOPE, replace(query, top_k=2))
    assert [item.unit_id for item in limited.items] == ["root", "second"]
    assert limited.items == full.items[:2], "最终裁剪必须保留完整排序的结果前缀"
    assert embedder.calls[-1] == [query.text, first.content, second.content]
