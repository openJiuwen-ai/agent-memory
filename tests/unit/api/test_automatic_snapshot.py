# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""公共写入入口使用同一策略开关与真实存储验证自动 Snapshot。"""

import asyncio
from copy import deepcopy
from datetime import timedelta

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.common.errors import PolicyError, ValidationError
from jiuwen_memory.common.type_def import HierarchyRole
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.control.policy import PolicyProducer
from jiuwen_memory.control.types import BatchWriteItem
from tests.unit.api.fixtures import (
    HOME,
    ROOT_SECURITY,
    START,
    runtime_config,
    security_for,
)

pytestmark = pytest.mark.unit


@pytest.fixture(params=["in_memory", "cloud"])
def api(request):
    runtime = assemble_runtime(config=runtime_config(request.param))
    try:
        yield runtime.api
    finally:
        runtime.close()


def test_switch_controls_snapshot_and_preserves_message_time(api):
    plain = api.add("plain", HOME, security=security_for(api, HOME))[0]
    assert plain.hierarchy.is_empty
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    metadata = {"session": "session_1", "observation_date": "2020-01-01"}
    before = deepcopy(metadata)
    unit = asyncio.run(api.add_async(
        "time evidence", HOME, security=security_for(api, HOME),
        system_metadata=metadata, occurred_at=START,
    ))[0]
    stored = api.get(unit.id, HOME, security=security_for(api, HOME))
    assert stored.hierarchy.role is HierarchyRole.SNAPSHOT
    assert stored.hierarchy.span_start == stored.hierarchy.span_end == START
    assert stored.temporal.t_message == START and stored.temporal.t_event is None
    assert stored.hierarchy.parent_id == "" and stored.scope == HOME
    assert metadata == before and stored.system_metadata["session"] == "session_1"
    api.admin_set("hierarchy.enabled", "false", security=ROOT_SECURITY)
    assert api.add("ordinary again", HOME, security=security_for(api, HOME))[0].hierarchy.is_empty


def test_batch_uses_each_effective_occurred_at_and_reports_missing_time(api):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    result = api.batch_add([
        BatchWriteItem(content="one", occurred_at=START),
        BatchWriteItem(content="two", occurred_at=START + timedelta(seconds=1)),
        BatchWriteItem(content="missing"),
    ], HOME, security=security_for(api, HOME))
    assert len(result.outcomes) == 3
    units = api.list(HOME, security=security_for(api, HOME)).items
    assert {unit.hierarchy.span_start for unit in units} == {START, START + timedelta(seconds=1)}
    assert result.outcomes[2].error_type == "ValidationError"


@pytest.mark.parametrize("infer", [False, True])
def test_infer_facts_inherit_time_without_extra_raw_snapshot(api, infer):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    units = api.add("Alice likes climbing", HOME, security=security_for(api, HOME),
                    occurred_at=START, system_metadata={"infer": infer})
    assert units
    assert all(unit.hierarchy.role is HierarchyRole.SNAPSHOT for unit in units)
    assert all(unit.hierarchy.span_start == START for unit in units)
    assert api.list(HOME, security=security_for(api, HOME)).count == len(units)


@pytest.mark.parametrize("hints", [
    {}, {"hierarchy_kind": "time", "hierarchy_role": "snapshot"},
])
def test_missing_spans_require_message_time(api, hints):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    with pytest.raises(ValidationError, match="occurred_at"):
        api.add("missing", HOME, security=security_for(api, HOME), system_metadata=hints)
    assert api.list(HOME, security=security_for(api, HOME)).count == 0


def test_explicit_complete_spans_win_and_partial_spans_fail(api):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    hints = {"hierarchy_kind": "time", "hierarchy_role": "snapshot",
             "hierarchy_span_start": START.isoformat(),
             "hierarchy_span_end": (START + timedelta(hours=1)).isoformat()}
    unit = api.add("explicit", HOME, security=security_for(api, HOME), system_metadata=hints)[0]
    assert unit.hierarchy.span_end == START + timedelta(hours=1)
    del hints["hierarchy_span_end"]
    with pytest.raises(ValidationError):
        api.add(
            "partial", HOME, security=security_for(api, HOME),
            occurred_at=START, system_metadata=hints,
        )
    assert api.list(HOME, security=security_for(api, HOME)).count == 1


@pytest.mark.parametrize("engine_kind", ["in_memory", "cloud"])
def test_configured_policy_table_replaces_defaults(engine_kind):
    config = runtime_config(engine_kind)
    config["globals"] = {"policies": {"rerank.enabled": "false", "hierarchy.enabled": "true"}}
    runtime = assemble_runtime(config=config)
    try:
        api = runtime.api
        assert api.admin_get("rerank.enabled", security=ROOT_SECURITY) == "false"
        with pytest.raises(PolicyError, match="unknown policy key"):
            api.admin_get("scope.require_space", security=ROOT_SECURITY)
        unit = api.add("configured", HOME, security=security_for(api, HOME), occurred_at=START)[0]
        assert unit.hierarchy.role is HierarchyRole.SNAPSHOT
        api.admin_set("hierarchy.enabled", "false", security=ROOT_SECURITY)
        assert api.add("plain", HOME, security=security_for(api, HOME))[0].hierarchy.is_empty
    finally:
        runtime.close()


@pytest.mark.parametrize("policies", [None, {}, {"rerank.enabled": "false"}])
def test_policy_builder_keeps_omitted_empty_and_custom_tables_distinct(policies):
    config = {} if policies is None else {"policies": policies}
    manager = PolicyProducer.build("dict", config, AssemblyContext())
    if policies is None:
        assert manager.get("hierarchy.enabled") == "false"
        assert manager.get("rerank.enabled") == "true"
    else:
        assert manager.all() == policies
        with pytest.raises(PolicyError, match="unknown policy key"):
            manager.set("hierarchy.enabled", "true")


@pytest.mark.parametrize("policies", [{}, {"rerank.enabled": "false"}])
def test_custom_policy_without_tree_key_keeps_ordinary_writes(policies):
    config = runtime_config("in_memory")
    config["globals"] = {"policies": policies}
    runtime = assemble_runtime(config=config)
    try:
        unit = runtime.api.add("ordinary", HOME, security=security_for(runtime.api, HOME))[0]
        assert unit.hierarchy.is_empty
    finally:
        runtime.close()
