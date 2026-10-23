# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""内部单层请求与完整只读子树证据。"""

from copy import deepcopy
from datetime import datetime, timedelta

from jiuwen_memory.common.type_def import HierarchyRole, MemoryUnit
from jiuwen_memory.construction.hierarchy_composer import (
    TIME_CHILD_ROLES,
    HierarchyComposeRequest,
    HierarchyIncrementalContext,
)
from tests.unit.construction.hierarchy_fixtures import ORIGIN, CompositionHarness, make_request


def incremental_request(
    inputs: list[MemoryUnit], *, role: HierarchyRole = HierarchyRole.TIME_SPAN,
    support: list[MemoryUnit] | None = None, now: datetime | None = None,
) -> HierarchyComposeRequest:
    """快照调用参数；调用者可再设置下层 ready_before 来验证封口。"""
    end = now if now is not None else ORIGIN + timedelta(days=4)
    request = make_request(deepcopy(inputs), span=(ORIGIN, end))
    request.options.leaf_role = TIME_CHILD_ROLES[role]
    request.options.parent_roles = [role]
    request.incremental = HierarchyIncrementalContext(end, supporting_units=deepcopy(support or []))
    return request


def subtree_evidence(harness: CompositionHarness, roots: list[MemoryUnit]) -> list[MemoryUnit]:
    """只经公开读取完整下层，根不重复包含在证据中。"""
    pending = list(roots)
    evidence = []
    while pending:
        parent = pending.pop()
        for position, uid in enumerate(parent.hierarchy.child_ids):
            scope = parent.hierarchy.child_scope_at(position, parent.scope)
            child = harness.read(scope, uid)
            evidence.append(child)
            pending.append(child)
    return evidence
