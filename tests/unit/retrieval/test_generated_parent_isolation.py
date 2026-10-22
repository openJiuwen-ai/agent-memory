# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""普通检索在索引准入及真源复核分别排除树父节点。"""

from copy import deepcopy

import pytest

from jiuwen_memory.common.type_def import HierarchyRef, HierarchyRole, ParsedQuery, and_merge
from jiuwen_memory.common.type_def.retrieval_filter import is_retrieval_candidate
from jiuwen_memory.retrieval.retriever_impl.predicate_builder import (
    build_ordinary_hierarchy_filters,
)
from tests.unit.construction.time_pipeline_fixtures import make_leaf
from tests.unit.storage.test_hierarchy_filters import search_ids

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("backend", ["fulltext", "vector-search", "vector-recall"])
def test_generated_parents_cannot_consume_ordinary_topk(backend):
    metadata = [{"hierarchy_kind": "time", "hierarchy_role": role}
                for role in ("event", "scene", "time_span", "snapshot")]
    metadata.append({})
    expr = and_merge(None, build_ordinary_hierarchy_filters())
    assert search_ids(backend, metadata, expr, limit=2) == ["3", "4"]


@pytest.mark.parametrize("role,accepted", [
    (HierarchyRole.SNAPSHOT, True), (None, True),
    (HierarchyRole.TIME_SPAN, False), (HierarchyRole.SCENE, False), (HierarchyRole.EVENT, False),
])
def test_truth_recheck_does_not_trust_stale_index_metadata(role, accepted):
    unit = deepcopy(make_leaf("leaf", 0))
    unit.hierarchy = HierarchyRef() if role is None else unit.hierarchy
    if role is not None:
        unit.hierarchy.role = role
    result = is_retrieval_candidate(unit, ParsedQuery(include_archived=True), filters=None)
    assert result is accepted
