# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""父层深度、重建及索引与真源一致性；共用真实持久化准备。"""

from copy import deepcopy

import pytest

from jiuwen_memory.common.type_def import HierarchyRole, LifecycleState, validate_tree
from jiuwen_memory.storage.types import IndexWriteMode
from tests.unit.construction.hierarchy_fixtures import (
    make_harness,
    make_leaf,
    make_request,
    replacement_request,
)

pytestmark = pytest.mark.unit
ROLES = [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.EVENT]


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_rebuild_preserves_cross_session_same_id_leaves_and_retires_all_old_parents(depth):
    leaves = [make_leaf("same"), make_leaf("same", 20)]
    leaves[1].scope.session = "s2"
    original = deepcopy(leaves)
    harness = make_harness(leaves)
    request = make_request(leaves)
    request.options.parent_roles = ROLES[:depth]
    first = harness.composer.build(request)
    assert first.complete
    old_parents = first.created_parent_ids
    for _ in range(2):
        replacement = replacement_request(harness, request, old_parents)
        replacement.options.parent_roles = ROLES[:depth]
        outcome = harness.composer.replace_in_span(replacement)
        assert outcome.complete
        assert set(outcome.replaced_parent_ids) == set(old_parents)
        assert not set(old_parents).intersection(outcome.created_parent_ids)
        parents = [harness.read(request.options.tree_home_scope, uid)
                   for uid in outcome.created_parent_ids]
        children = [harness.read(leaf.scope, leaf.id) for leaf in leaves]
        validate_tree([*parents, *children])
        assert parents[0].hierarchy.role is ROLES[depth - 1]
        assert {p.hierarchy.role for p in parents} == set(ROLES[:depth])
        for before, current in zip(original, children):
            current.hierarchy = deepcopy(before.hierarchy)
            assert current == before
        for uid in old_parents:
            parent = harness.read(request.options.tree_home_scope, uid)
            assert parent.lifecycle is LifecycleState.ARCHIVED
            assert not parent.hierarchy.parent_id and not parent.hierarchy.child_ids
            assert not harness.manager.fulltext().get(parent.scope, [uid])
        for parent in parents:
            document = harness.manager.fulltext().get(parent.scope, [parent.id])[0]
            assert document.metadata["tree_parent_id"] == parent.hierarchy.parent_id
        old_parents = outcome.created_parent_ids
    root_writes = [call for call in harness.builder.calls
                   if call.method == "build" and call.mode is IndexWriteMode.FORWARD_ONLY]
    assert root_writes[0].units[0].hierarchy.role is ROLES[depth - 1]
