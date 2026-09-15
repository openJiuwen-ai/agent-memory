# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compact API/engine regression for issue #208 and request-local Schema updates."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
from jiuwen_memory.common.errors import (
    BackendError,
    NotFoundError,
    PartialFailureError,
    PermissionDeniedError,
)
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.security.types import Action, Grant
from jiuwen_memory.common.type_def import Context, EntityBatchResult, LifecycleState, Scope
from jiuwen_memory.construction.extractor_impl.entity_schema_extractor import (
    InvalidSchemaExtractionError,
)
from jiuwen_memory.construction.source_update import STRICT_ENTITY_WRITES
from jiuwen_memory.control.types import MemoryPatch, UpdateMode
from tests.unit.control.fixtures import T0, T1, SchemaWorld

pytestmark = pytest.mark.unit


@pytest.fixture(params=["in_memory", "cloud"])
def world(request, tmp_path, monkeypatch):
    result = SchemaWorld(tmp_path, monkeypatch, request.param)
    yield result
    result.kernel.ingest_jobs.close(wait=True)


@pytest.mark.parametrize("mode", list(UpdateMode))
def test_issue208_refreshes_entities_properties_and_search(world, mode):
    source, old_props = world.add("陈静负责推荐算法迭代")
    world.llm.facts = [("陈静", "review", "陈静上周评审了召回方案的设计文档", "")]
    sentinel, _ = world.add("陈静上周评审了召回方案的设计文档")
    raw_messages = dict(world.kernel.kv.scan(world.scope, "/messages/"))
    world.llm.facts = [("李红", "occupation", "李红负责推荐算法迭代", "")]

    updated = world.update(
        source, MemoryPatch(content="李红负责推荐算法迭代", mode=mode, t_valid=T1)
    )

    assert world.get(updated.id).entities == ["李红"]
    assert world.linked("李红") == {updated.id}
    assert sentinel.id in world.linked("陈静")
    assert updated.id not in world.linked("陈静")
    assert world.get(sentinel.id).content == sentinel.content
    assert dict(world.kernel.kv.scan(world.scope, "/messages/")) == raw_messages
    assert world.kernel.kv.scan(world.scope, "/schema_updates/") == []
    props = [
        u for u in world.units()
        if u.provenance == [updated.id] and u.system_metadata.get("schema_entity_name") == "李红"
    ]
    assert len(props) == 1 and props[0].entities == []
    assert props[0].system_metadata["schema_property_name"] == "occupation"
    assert not props[0].supersedes  # Changing the person must not invent a property version chain.
    if mode is UpdateMode.OVERWRITE:
        assert updated.id == source.id
        with pytest.raises(NotFoundError):
            world.get(old_props[0].id)
        assert world.linked("陈静") == {sentinel.id}
    else:
        assert updated.supersedes == source.id
        assert world.get(source.id).lifecycle is LifecycleState.SUPERSEDED
        assert world.get(old_props[0].id).temporal.t_invalid == T1
        assert world.get(updated.id, T0 + timedelta(days=1)).content == source.content
    assert len(world.units()) > 2
    new_hits = world.api.search(
        "李红负责推荐算法迭代", Context(scope=world.scope), top_k=2, security=world.security
    )
    old_hits = world.api.search(
        "陈静", Context(scope=world.scope), top_k=2, security=world.security
    )
    assert updated.id in {item.unit_id for item in new_hits.items}
    assert updated.id not in {item.unit_id for item in old_hits.items}


@pytest.mark.parametrize("mode", list(UpdateMode))
def test_empty_content_retracts_entities_and_single_source_property(world, mode):
    source, props = world.add("陈静负责推荐算法迭代")
    updated = world.update(source, MemoryPatch(content="", mode=mode, t_valid=T1))
    assert world.get(updated.id).entities == []
    assert updated.id not in world.linked("陈静")
    if mode is UpdateMode.OVERWRITE:
        with pytest.raises(NotFoundError):
            world.get(props[0].id)
    else:
        assert world.get(props[0].id).temporal.t_invalid == T1


@pytest.mark.parametrize(
    ("response", "error"),
    [("broken json", InvalidSchemaExtractionError), (TimeoutError("LLM timeout"), TimeoutError)],
)
def test_failed_extraction_does_not_change_memory_or_entities(world, response, error):
    source, _ = world.add("陈静负责推荐算法迭代")
    before = world.snapshot()
    world.llm.response = response
    with pytest.raises(error):
        world.update(source, MemoryPatch(content="李红负责算法"))
    assert world.snapshot() == before
    assert world.linked("陈静") == {source.id}
    assert world.linked("李红") == set()
    assert world.kernel.kv.scan(world.scope, "/schema_updates/") == []


@pytest.mark.parametrize("denied_action", [Action.WRITE, Action.DELETE])
def test_update_only_grant_cannot_create_or_delete_properties(world, denied_action):
    source, _ = world.add("陈静负责推荐算法迭代")
    actor = Scope(org=world.scope.org, user="editor")
    world.api._perm.grant(Grant(grantor=world.scope, grantee=actor, actions={Action.UPDATE}))
    world.llm.facts = [("李红", "occupation", "李红负责算法", "")]
    content = "李红负责算法" if denied_action is Action.WRITE else ""
    before = world.snapshot()
    with pytest.raises(PermissionDeniedError, match=denied_action.value):
        world.api.update(
            source.id,
            world.scope,
            MemoryPatch(content=content, mode=UpdateMode.OVERWRITE),
            security=legacy_request_context(actor),
        )
    assert world.snapshot() == before
    assert world.linked("陈静") == {source.id}
    assert world.kernel.kv.scan(world.scope, "/schema_updates/") == []


def test_index_failure_reports_partial_write_without_persistent_recovery(world, monkeypatch):
    source, _ = world.add("陈静负责推荐算法迭代")
    world.llm.facts = [("李红", "occupation", "李红负责算法", "")]
    monkeypatch.setattr(
        world.entity_store,
        "execute_operations",
        Mock(return_value=EntityBatchResult(successful_ids=[], failed_ids=["unavailable"])),
    )
    with pytest.raises(PartialFailureError) as caught:
        world.update(source, MemoryPatch(content="李红负责算法", mode=UpdateMode.OVERWRITE))
    assert caught.value.retry_action == "inspect affected records before another update"
    assert isinstance(caught.value.__cause__, BackendError)
    assert any(u.content == "李红负责算法" for u in world.units())
    assert world.kernel.kv.scan(world.scope, "/schema_updates/") == []
    assert STRICT_ENTITY_WRITES.get() is False


@pytest.mark.parametrize("engine_kind", ["in_memory", "cloud"])
@pytest.mark.parametrize("mode", list(UpdateMode))
def test_schema_disabled_keeps_original_update_path(engine_kind, mode, monkeypatch):
    monkeypatch.setattr("jiuwen_memory.api.memory_api_impl.assembly.setup_logging", lambda _: None)
    kernel = _build_kernel(
        config={
            "globals": {"schema_enabled": False, "graph_enabled": False, "rerank_enabled": False},
            "engine": {"default": {"target": engine_kind}},
        }
    )
    try:
        api, engine = kernel.api, kernel.api._engine
        scope = Scope(org="ordinary", user="alice")
        security = legacy_request_context(scope)
        source = api.add("Original", scope, security=security)[0]
        prepare = Mock(side_effect=AssertionError("ordinary update must bypass Schema preparation"))
        load = Mock(wraps=engine._load)
        route = Mock(wraps=engine._write_binding)
        monkeypatch.setattr(engine, "prepare_update", prepare)
        monkeypatch.setattr(engine, "_load", load)
        monkeypatch.setattr(engine, "_write_binding", route)
        updated = api.update(
            source.id, scope, MemoryPatch(content="Updated", mode=mode), security=security
        )
        assert updated.content == "Updated"
        assert (updated.id == source.id) == (mode is UpdateMode.OVERWRITE)
        prepare.assert_not_called()
        assert load.call_count == 3  # Permission context, audit snapshot, original Engine update.
        assert route.call_count == (2 if engine_kind == "cloud" else 0)
    finally:
        kernel.ingest_jobs.close(wait=True)
