# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""自动候选分类、旧中间层完整证据与同源 DomainStore 读取。"""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.type_def import HierarchyRole, memory_key
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.control.jobs_impl.hierarchy_candidates import HierarchyJobLimits
from jiuwen_memory.control.jobs_impl.hierarchy_incremental import collect_incremental
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.types import MemoryListResult
from tests.unit.construction.event_fixtures import EVENT_ROLES
from tests.unit.construction.hierarchy_fixtures import ORIGIN, make_leaf, make_request
from tests.unit.control.hierarchy_job_fixtures import job_harness

pytestmark = pytest.mark.unit


def _select(harness, *, role=HierarchyRole.SNAPSHOT, now=None, limit=5000, domain=False):
    end = now or ORIGIN + timedelta(days=8)
    parent = {HierarchyRole.SNAPSHOT: HierarchyRole.TIME_SPAN,
              HierarchyRole.TIME_SPAN: HierarchyRole.SCENE}[role]
    options = replace(harness.options, leaf_role=role, parent_roles=[parent],
                      span_start=end - timedelta(days=7), span_end=end)
    store = harness.kv
    if domain:
        store = Mock(spec=DomainStore)
        store.scopes.side_effect = harness.kv.scopes

        def list_units(scope, *, offset=0, limit=100):
            page = harness.kv.list(scope, offset=offset, limit=limit)
            return MemoryListResult(items=[loads(raw) for _, raw in page.entries], count=page.count)

        store.list.side_effect = list_units
        store.get.side_effect = lambda scope, ids: [
            harness.composition.read(scope, uid) for uid in reversed(ids)
        ]
    return collect_incremental(store, options, tuple(EVENT_ROLES),
                               HierarchyJobLimits(max_leaves=limit, page_size=2))


@pytest.mark.parametrize("domain", [False, True])
def test_expired_and_incomplete_inputs_do_not_consume_quota(domain):
    old = [make_leaf(f"old-{i}") for i in range(25)]
    missing = make_leaf("missing")
    missing.hierarchy.span_end = None
    fresh = make_leaf("fresh", 8 * 1440)
    harness = job_harness([*old, missing, fresh])
    selected = _select(harness, limit=1, domain=domain)
    assert [unit.id for unit in selected.inputs] == [fresh.id]
    assert selected.skipped_outside_window_count == 25
    assert selected.skipped_incomplete_time_count == 1
    assert selected.needs_rebuild_count == 25
    assert len(selected.skipped_samples) == 20
    assert selected.pending_before is None
    assert harness.read(missing) == missing


@pytest.mark.parametrize("offset", [-1, 0])
def test_late_watermark_is_inclusive_and_does_not_block_healthy_input(offset):
    original = make_leaf("original")
    harness = job_harness([original])
    harness.composition.composer.build(make_request([original]))
    late, healthy = make_leaf("late", offset), make_leaf("healthy", 1)
    harness.composition.builder.build([late, healthy])
    selected = _select(harness, now=ORIGIN + timedelta(hours=1))
    assert [unit.id for unit in selected.inputs] == [healthy.id]
    assert selected.skipped_late_count == 1
    assert harness.read(late).hierarchy.parent_id == ""


@pytest.mark.parametrize("domain", [False, True])
def test_middle_root_and_complete_descendants_survive_lookback(domain):
    leaves = [make_leaf("first"), make_leaf("second", 1)]
    harness = job_harness(leaves)
    result = harness.composition.composer.build(make_request(leaves))
    selected = _select(harness, role=HierarchyRole.TIME_SPAN, domain=domain)
    assert [unit.id for unit in selected.inputs] == result.created_parent_ids
    assert {unit.id for unit in selected.supporting_units} == {leaf.id for leaf in leaves}
    assert selected.skipped_outside_window_count == 0
    assert all(unit.hierarchy.parent_id == "" for unit in selected.inputs)


def test_future_input_preserves_pending_frontier_and_limits():
    future = make_leaf("future", 8 * 1440 + 1)
    harness = job_harness([future])
    selected = _select(harness)
    assert not selected.inputs
    assert selected.pending_before == future.hierarchy.span_start
    harness.composition.builder.build([make_leaf("more", 8 * 1440 + 2)])
    with pytest.raises(ValidationError, match="max_leaves"):
        _select(harness, limit=1)


def test_missing_child_is_not_treated_as_an_ineligible_candidate():
    original = make_leaf("original")
    harness = job_harness([original])
    harness.composition.composer.build(make_request([original]))
    harness.kv.delete(original.scope, memory_key(original.id))
    with pytest.raises(NotFoundError):
        _select(harness, role=HierarchyRole.TIME_SPAN)
