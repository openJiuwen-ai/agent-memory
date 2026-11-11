# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""阶段契约：默认三路轨迹、覆写、安全边界与延迟展开。"""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterOp,
    HierarchyRole,
    LifecycleState,
    RecallChannel,
    RetrievalPipeline,
)
from jiuwen_memory.retrieval.expansion import PreparedRetrievalResult, complete_expansion
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.staged_retriever import RetrieveState, StagedRetriever
from tests.unit.retrieval.expansion_fixtures import make_tree
from tests.unit.retrieval.hierarchy_query_fixtures import (
    QUERY_SCOPE,
    hierarchy_query,
    make_harness,
    tree_unit,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
@pytest.mark.parametrize("degraded", [False, True])
def test_stage_sequence_and_rerank_degradation(pipeline, degraded):
    harness = make_harness(pipeline)
    harness.add([tree_unit("parent")])
    reranker = Mock()
    reranker.rerank.side_effect = BackendError("offline") if degraded else None
    reranker.rerank.return_value = [0.8]
    retriever = PipelineRetriever(
        harness.parser, RRFFuser(), harness.discloser, None, reranker,
        domain_store=harness.domain,
    )
    result = retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        with_trajectory=True, channels=[RecallChannel.KEYWORD],
    ))
    assert isinstance(retriever, StagedRetriever)
    assert [step.stage for step in result.trajectory] == [
        "parse", "recall", "fuse", "recheck", "rerank", "threshold", "disclose",
    ]
    assert [item.unit_id for item in result.items] == ["parent"]
    threshold = next(step for step in result.trajectory if step.stage == "threshold")
    assert threshold.detail["calibrated"] == str(not degraded)
    recall = next(step for step in result.trajectory if step.stage == "recall")
    assert recall.detail["pipeline"] == pipeline.value


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
def test_stage_override_preserves_filtering_lifecycle_and_scope(pipeline):
    class ReverseRerank(PipelineRetriever):
        def rerank(self, state):
            state.candidates = list(reversed(state.candidates))

    harness = make_harness(pipeline, recall_limit=1)
    excluded = tree_unit("excluded")
    excluded.tags = ["other"]
    archived = tree_unit("archived")
    archived.tags = ["wanted"]
    archived.lifecycle = LifecycleState.ARCHIVED
    foreign = tree_unit("foreign")
    foreign.scope = replace(QUERY_SCOPE, user="other")
    foreign.tags = ["wanted"]
    included = tree_unit("included")
    included.tags = ["wanted"]
    # 插入顺序让过滤后置的实现必定丢掉合法命中。
    harness.add([excluded, archived, included])
    harness.domain.add(foreign.scope, [foreign])
    harness.keyword_builder.build([foreign])
    harness.vector_builder.build([foreign])
    retriever = ReverseRerank(
        harness.parser, RRFFuser(), harness.discloser, None,
        recall_max=1, domain_store=harness.domain,
    )
    result = retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        top_k=1, filters=[FilterClause("tags", FilterOp.CONTAINS, "wanted")],
    ))
    assert [item.unit_id for item in result.items] == ["included"]
    # 索引仍声称 included 有效，真源已归档；不得披露这个旧命中。
    included.lifecycle = LifecycleState.ARCHIVED
    harness.domain.update(QUERY_SCOPE, [included])
    stale = retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        top_k=1, filters=[FilterClause("tags", FilterOp.CONTAINS, "wanted")],
    ))
    assert stale.items == []


def test_rollup_stage_restores_role_and_delayed_expansion_skips_finalize():
    tree = make_tree(index_children=True)

    class FinalizeSpy(PipelineRetriever):
        def finalize(self, state):
            raise AssertionError("deferred expansion must bypass finalize")

    retriever = FinalizeSpy(
        tree.base.parser, RRFFuser(), tree.base.discloser, None,
        domain_store=tree.base.domain,
    )
    retriever.bind_expander(tree.expander)
    query = hierarchy_query(rollup=True, expand_depth=1, top_k=3, defer_expansion=True)
    prepared = retriever.retrieve(QUERY_SCOPE, query)
    assert isinstance(prepared, PreparedRetrievalResult)
    expected = tree.base.retriever.retrieve(QUERY_SCOPE, replace(query, defer_expansion=False))
    completed = complete_expansion(prepared, [prepared], query)
    assert completed.items == expected.items
    state = RetrieveState.start(QUERY_SCOPE, query)
    retriever.validate(state)
    assert retriever.parse(state)
    assert state.parsed.hierarchy_role is None
    retriever.build_filters(state)
    retriever.select_channels(state)
    retriever.plan_budget(state)
    retriever.recall_and_fuse(state)
    state.cut_to_budget()
    snapshot = list(state.recalled_children)
    retriever.rerank(state)
    retriever.rollup(state)
    assert state.parsed.hierarchy_role is HierarchyRole.TIME_SPAN
    assert state.recalled_children == snapshot
    assert state.result is None
