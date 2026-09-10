"""上卷在真实三条检索链路中准入父节点，不依赖父自身命中索引。"""

from dataclasses import replace
from functools import partial
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import SearchOptions
from jiuwen_memory.common.errors import UnsupportedCapabilityError, ValidationError
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterOp,
    HierarchyKind,
    HierarchyRole,
    RecallChannel,
    RetrievalPipeline,
    Segment,
)
from jiuwen_memory.retrieval.discloser_impl.structured_discloser import StructuredDiscloser
from jiuwen_memory.retrieval.discloser_impl.truncating_discloser import TruncatingDiscloser
from jiuwen_memory.retrieval.expander_impl.default_expander import DefaultExpander
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.fuser_impl.score_max_fuser import ScoreMaxFuser
from jiuwen_memory.retrieval.fuser_impl.weighted_rrf_fuser import WeightedRRFFuser
from jiuwen_memory.retrieval.retriever_impl.multimodal_retriever import MultimodalRetriever
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.types import DisclosureLevel, RetrievalQuery
from tests.unit.retrieval.expansion_fixtures import link
from tests.unit.retrieval.hierarchy_query_fixtures import (
    QUERY_SCOPE,
    hierarchy_query,
    make_harness,
    tree_unit,
)
from tests.unit.retrieval.rollup_fixtures import dropped_filters_parser

pytestmark = pytest.mark.unit


def test_rollup_defaults_off_and_requires_explicit_kind() -> None:
    assert SearchOptions().rollup is False
    assert RetrievalQuery().rollup is False
    with pytest.raises(ValidationError, match="hierarchy_kind"):
        RetrievalQuery(rollup=True)


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_rollup_rejects_non_boolean(value) -> None:
    with pytest.raises(ValidationError, match="rollup"):
        RetrievalQuery(hierarchy_kind=HierarchyKind.TIME, rollup=value)


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
def test_descendant_admits_unindexed_parent_and_expansion_is_independent(pipeline) -> None:
    harness = make_harness(pipeline)
    parent = tree_unit("unindexed-parent")
    children = [tree_unit(f"leaf-{index}", HierarchyRole.SNAPSHOT) for index in range(2)]
    link(parent, children)
    harness.domain.add(QUERY_SCOPE, [parent])
    harness.add(children)
    harness.retriever.bind_expander(DefaultExpander(harness.domain))
    query = hierarchy_query(rollup=True, with_trajectory=True)
    result = harness.retriever.retrieve(QUERY_SCOPE, query)
    assert [item.unit_id for item in result.items] == [parent.id]
    assert result.errors == []
    assert any(step.stage == "rollup" for step in result.trajectory)
    assert query.hierarchy_role is HierarchyRole.TIME_SPAN
    assert harness.retriever.retrieve(QUERY_SCOPE, replace(query, rollup=False)).items == []
    expanded = harness.retriever.retrieve(QUERY_SCOPE, replace(query, expand_depth=1))
    assert [item.unit_id for item in expanded.items] == [parent.id, "leaf-0", "leaf-1"]


def test_without_role_adds_only_direct_parent_and_keeps_direct_matches() -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    root = tree_unit("scene", HierarchyRole.SCENE)
    parent = tree_unit("time-span")
    child = tree_unit("leaf", HierarchyRole.SNAPSHOT)
    link(root, [parent])
    link(parent, [child])
    harness.domain.add(QUERY_SCOPE, [root, parent])
    harness.add([child])
    result = harness.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        rollup=True, hierarchy_role=None,
    ))
    assert {item.unit_id for item in result.items} == {parent.id, child.id}
    result = harness.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        rollup=True, hierarchy_role=HierarchyRole.SCENE,
    ))
    assert [item.unit_id for item in result.items] == [root.id]


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
@pytest.mark.parametrize("fuser_type", [RRFFuser, WeightedRRFFuser, ScoreMaxFuser])
@pytest.mark.parametrize("use_reranker", [False, True])
def test_rollup_uses_common_final_scale_before_threshold_and_top_k(
    pipeline, fuser_type, use_reranker,
) -> None:
    """父低分、子高分；父直接命中时也不得被精排重新覆盖传播分。"""
    harness = make_harness(pipeline)
    parent = tree_unit("parent")
    child = tree_unit("leaf", HierarchyRole.SNAPSHOT)
    parent.segments = [Segment(content="recall evidence parent")]
    child.segments = [Segment(content="recall evidence leaf")]
    link(parent, [child])
    harness.add([parent, child])
    reranker = Mock()
    reranker.rerank.side_effect = lambda query, texts: [
        0.9 if "leaf" in text else 0.1 for text in texts
    ]
    retriever = PipelineRetriever(
        harness.parser, fuser_type(), TruncatingDiscloser(), None,
        reranker if use_reranker else None, min_score=0.5, domain_store=harness.domain,
    )
    baseline = retriever.retrieve(QUERY_SCOPE, hierarchy_query(hierarchy_role=None))
    result = retriever.retrieve(QUERY_SCOPE, hierarchy_query(rollup=True, top_k=1))
    assert [item.unit_id for item in result.items] == [parent.id]
    assert result.items[0].score == max(item.score for item in baseline.items)
    if use_reranker:
        assert result.items[0].score == 0.9
        assert reranker.rerank.call_count == 2
        assert set(reranker.rerank.call_args.args[1]) == {parent.content, child.content}


@pytest.mark.parametrize("channel", [RecallChannel.KEYWORD, RecallChannel.VECTOR])
def test_rollup_does_not_require_a_specific_recall_channel(channel) -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    parent = tree_unit("parent")
    child = tree_unit("child", HierarchyRole.SNAPSHOT)
    link(parent, [child])
    harness.domain.add(QUERY_SCOPE, [parent])
    harness.add([child])
    result = harness.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        rollup=True, channels=[channel],
    ))
    assert [item.unit_id for item in result.items] == [parent.id]


@pytest.mark.parametrize("discloser_type", [StructuredDiscloser, TruncatingDiscloser])
@pytest.mark.parametrize("depth", [0, 1])
def test_same_id_promoted_parents_keep_content_and_expansion_source(discloser_type, depth) -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    parents = [tree_unit("parent"), tree_unit("parent")]
    leaves = [tree_unit(f"leaf-{index}", HierarchyRole.SNAPSHOT) for index in range(2)]
    for parent_index, parent in enumerate(parents):
        parent.scope = replace(QUERY_SCOPE, session=str(parent_index))
        parent.segments = [Segment(content=f"parent content {parent_index}")]
        link(parent, [leaves[parent_index]])
        harness.domain.add(parent.scope, [parent])
    harness.add(leaves)
    retriever = PipelineRetriever(
        harness.parser, RRFFuser(), discloser_type(), None, domain_store=harness.domain,
    )
    retriever.bind_expander(DefaultExpander(harness.domain))
    result = retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        rollup=True, expand_depth=depth, disclosure=DisclosureLevel.ADAPTIVE, max_tokens=10000,
    ))
    assert len(result.items) == 2 + 2 * depth
    assert "parent content 0" in result.items[0].content
    assert "parent content 1" in result.items[1].content
    if depth:
        assert {item.unit_id for item in result.items[2:]} == {leaf.id for leaf in leaves}
    assert result.errors == []


def test_original_filters_survive_a_parser_that_drops_them() -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    parent = tree_unit("denied-parent")
    child = tree_unit("allowed-leaf", HierarchyRole.SNAPSHOT)
    child.user_metadata["allowed"] = True
    link(parent, [child])
    harness.domain.add(QUERY_SCOPE, [parent])
    harness.add([child])
    parser = Mock()
    parser.parse.side_effect = partial(dropped_filters_parser, harness.parser)
    retriever = PipelineRetriever(
        parser, RRFFuser(), TruncatingDiscloser(), None, domain_store=harness.domain,
    )
    result = retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        rollup=True, filters=[FilterClause("user_metadata.allowed", FilterOp.EQ, True)],
    ))
    assert result.items == []
    assert result.trajectory == []
    assert [(error.source, error.error_type) for error in result.errors] == [
        ("rollup", "visibility_excluded"),
    ]


def test_multimodal_wrapper_explicitly_rejects_rollup() -> None:
    harness = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    wrapper = MultimodalRetriever(harness.retriever)
    with pytest.raises(UnsupportedCapabilityError, match="rollup"):
        wrapper.retrieve(QUERY_SCOPE, hierarchy_query(rollup=True))
