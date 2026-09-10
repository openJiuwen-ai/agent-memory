# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""不允许使用未绑定实际记忆类型的 fallback 授权执行建树。"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import (
    Channel,
    EvolveMode,
    EvolveTaskOptions,
    HierarchyComposeOptions,
    HierarchyKind,
    HierarchyRole,
    Scope,
    ValidationError,
    assemble_runtime,
    legacy_request_context,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.construction.evolver import Evolver, EvolverProducer
from jiuwen_memory.control.scheduler import Scheduler, SchedulerProducer

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("route_key", ["memory_type", "pipeline", "resource_type", "sensitivity"])
def test_hierarchy_refuses_permission_routing_before_submission(
    monkeypatch, route_key: str
) -> None:
    """路由字段非空时在提交前拒绝，真实叶内容和可见列表保持不变。"""
    scheduler = Mock(spec=Scheduler)
    evolver = Mock(spec=Evolver)
    monkeypatch.setattr(SchedulerProducer, "build_named", lambda _name, _context: scheduler)
    monkeypatch.setattr(EvolverProducer, "build_named", lambda _name, _context: evolver)
    scope = Scope(org="acme", user="alice")
    security = legacy_request_context(scope)
    runtime = assemble_runtime(config={
        "permission": {
            "default": {
                "target": "routing",
                "params": {
                    "route_key": route_key,
                    "fallback": "strict",
                    "routes": {"sensitive": "strict"},
                },
            },
            "strict": "sqlite",
        },
    })
    try:
        runtime.api.admin_set(
            "hierarchy.enabled", "true", security=legacy_request_context(Scope())
        )
        leaves = runtime.api.add(
            "sensitive snapshot",
            scope,
            security=security,
            system_metadata={
                "hierarchy_kind": "time",
                "hierarchy_role": "snapshot",
                "hierarchy_span_start": "2026-09-10T09:30:00+00:00",
                "hierarchy_span_end": "2026-09-10T09:30:00+00:00",
                route_key: "sensitive",
            },
        )
        assert len(leaves) == 1
        original = runtime.api.get(leaves[0].id, scope, security=security)
        original_listing = runtime.api.list(scope, security=security)
        options = EvolveTaskOptions(
            mode=EvolveMode.HIERARCHY,
            channel=Channel.HOT,
            hierarchy_options=HierarchyComposeOptions(
                kind=HierarchyKind.TIME,
                leaf_role=HierarchyRole.SNAPSHOT,
                parent_roles=[HierarchyRole.TIME_SPAN],
                tree_home_scope=scope,
                span_start=datetime(2026, 9, 10, 9, tzinfo=timezone.utc),
                span_end=datetime(2026, 9, 10, 10, tzinfo=timezone.utc),
            ),
        )

        with pytest.raises(ValidationError, match="HIERARCHY 暂不支持权限路由"):
            runtime.api.evolve(scope, options, security=security)

        scheduler.submit.assert_not_called()
        evolver.evolve.assert_not_called()
        assert runtime.api.get(leaves[0].id, scope, security=security) == original
        assert runtime.api.list(scope, security=security) == original_listing
    finally:
        runtime.close()
        Factory.reset_all()
