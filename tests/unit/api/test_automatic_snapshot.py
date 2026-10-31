# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""公共写入入口使用同一策略开关与真实存储验证自动 Snapshot。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
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


def test_batch_preserves_each_time_and_accepts_missing_time(api):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    result = api.batch_add([
        BatchWriteItem(content="one", occurred_at=START),
        BatchWriteItem(content="two", occurred_at=START + timedelta(seconds=1)),
        BatchWriteItem(content="missing"),
    ], HOME, security=security_for(api, HOME))
    assert len(result.outcomes) == 3
    units = api.list(HOME, security=security_for(api, HOME)).items
    assert {unit.hierarchy.span_start for unit in units} == {
        START, START + timedelta(seconds=1), None,
    }
    assert all(not outcome.error_type for outcome in result.outcomes)
    missing = next(unit for unit in units if unit.content == "missing")
    assert missing.hierarchy.is_empty and missing.temporal.t_message is None


@pytest.mark.parametrize("infer", [False, True])
def test_infer_facts_inherit_time_without_extra_raw_snapshot(api, infer):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    units = api.add("Alice likes climbing", HOME, security=security_for(api, HOME),
                    occurred_at=START, system_metadata={"infer": infer})
    assert units
    assert all(unit.hierarchy.role is HierarchyRole.SNAPSHOT for unit in units)
    assert all(unit.hierarchy.span_start == START for unit in units)
    assert api.list(HOME, security=security_for(api, HOME)).count == len(units)


@pytest.mark.parametrize("infer", [False, True])
@pytest.mark.parametrize("span", ["plain", "neither", "start", "end"])
def test_missing_time_preserves_input_through_write_and_infer(api, infer, span):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    hints = {"infer": infer}
    if span != "plain":
        hints.update(hierarchy_kind="time", hierarchy_role="snapshot")
    if span in ("start", "end"):
        hints[f"hierarchy_span_{span}"] = START.isoformat()
    before = deepcopy(hints)
    units = api.add(
        "Alice likes climbing", HOME, security=security_for(api, HOME), system_metadata=hints,
    )
    assert units and hints == before
    for unit in units:
        stored = api.get(unit.id, HOME, security=security_for(api, HOME))
        assert stored.hierarchy.is_empty == (span == "plain")
        assert stored.hierarchy.span_start == (START if span == "start" else None)
        assert stored.hierarchy.span_end == (START if span == "end" else None)
        assert stored.temporal.t_message is None
        assert stored.hierarchy.parent_id == "" and stored.hierarchy.child_ids == []


def test_explicit_spans_win_and_occurred_at_does_not_repair_partial_input(api):
    api.admin_set("hierarchy.enabled", "true", security=ROOT_SECURITY)
    hints = {"hierarchy_kind": "time", "hierarchy_role": "snapshot",
             "hierarchy_span_start": START.isoformat(),
             "hierarchy_span_end": (START + timedelta(hours=1)).isoformat()}
    unit = api.add("explicit", HOME, security=security_for(api, HOME), system_metadata=hints)[0]
    assert unit.hierarchy.span_end == START + timedelta(hours=1)
    del hints["hierarchy_span_end"]
    partial = api.add("partial", HOME, security=security_for(api, HOME),
                      occurred_at=START + timedelta(seconds=5), system_metadata=hints)[0]
    stored = api.get(partial.id, HOME, security=security_for(api, HOME))
    assert stored.hierarchy.span_start == START and stored.hierarchy.span_end is None
    assert stored.temporal.t_message == START + timedelta(seconds=5)
    assert api.list(HOME, security=security_for(api, HOME)).count == 2


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


@pytest.mark.parametrize("infer", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_disabled_write_ignores_leaf_hints_without_changing_input(api, infer, partial):
    hints = {"hierarchy_kind": "time", "hierarchy_role": "snapshot",
             "hierarchy_span_start": START.isoformat(), "infer": infer,
             "parent_id": "business-parent", "span_start": "business-start"}
    if not partial:
        hints["hierarchy_span_end"] = START.isoformat()
    before = deepcopy(hints)
    security = security_for(api, HOME)
    plain = api.add("Alice likes climbing", replace(HOME, session="baseline"),
                    security=security, occurred_at=START,
                    system_metadata={key: value for key, value in hints.items()
                                     if not key.startswith("hierarchy_")})
    units = api.add("Alice likes climbing", HOME, security=security, occurred_at=START,
                    system_metadata=hints)
    assert units and hints == before
    assert all(unit.hierarchy.is_empty for unit in units)
    assert [unit.temporal.t_message for unit in units] == [
        unit.temporal.t_message for unit in plain
    ]
    assert all(not key.startswith("hierarchy_") for unit in units for key in unit.system_metadata)
    if not infer:
        assert units[0].temporal.t_message == START
        assert units[0].system_metadata["parent_id"] == "business-parent"
        assert units[0].system_metadata["span_start"] == "business-start"


@pytest.mark.parametrize("key", ["hierarchy_parent_id", "hierarchy_child_ids", "tree_parent_id",
                                 "hierarchy_status", "tree_span_start", "tree_span_end"])
def test_disabled_write_still_rejects_structural_and_projection_fields(api, key):
    with pytest.raises(ValidationError):
        api.add("forged", HOME, security=security_for(api, HOME), system_metadata={key: "fake"})
