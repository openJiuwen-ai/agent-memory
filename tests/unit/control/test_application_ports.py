"""B-03：Control application ports 可脱离 LocalMemoryAPI 直接验证。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from jiuwen_memory.common.errors import PartialFailureError
from jiuwen_memory.common.type_def import MemoryUnit, Modality, Scope, Segment
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control.application import (
    GovernanceService,
    MemoryCommandService,
    MemoryQueryService,
    SpaceLifecycleService,
)
from jiuwen_memory.control.types import (
    BatchWriteItem,
    BatchWriteOutcome,
    BatchWriteResult,
    Channel,
    DeleteSelector,
    MemoryListResult,
    MemoryPatch,
    PermissionContext,
    SpaceDeleteResult,
    SpaceStatus,
)
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", user="alice", space="coding")
_NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


def _unit(unit_id: str = "u1") -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=_SCOPE,
        segments=[Segment(content="hello")],
    )


def test_command_write_delegates_to_engine_without_local_memory_api() -> None:
    engine = AsyncMock()
    engine.write.return_value = [_unit()]
    service = MemoryCommandService(engine)

    units = asyncio.run(
        service.write(
            "hello",
            _SCOPE,
            Modality.TEXT,
            assets=["a"],
            tags=["t"],
            system_metadata={"k": "v"},
            user_metadata={"u": "1"},
            occurred_at=_NOW,
        )
    )

    assert units == [_unit()], "command.write must return engine.write result unchanged"
    engine.write.assert_awaited_once_with(
        "hello",
        _SCOPE,
        Modality.TEXT,
        assets=["a"],
        tags=["t"],
        system_metadata={"k": "v"},
        user_metadata={"u": "1"},
        occurred_at=_NOW,
    )


def test_command_batch_update_delete_evolve_delegate_to_engine() -> None:
    engine = AsyncMock()
    items = [BatchWriteItem(content="a", scope=_SCOPE)]
    batch_result = BatchWriteResult()
    engine.batch_write.return_value = batch_result
    patch = MemoryPatch(content="patched")
    engine.update.return_value = _unit("u2")
    selector = DeleteSelector(unit_ids=["u1"], scope=_SCOPE)
    engine.delete.return_value = ["u1"]
    engine.evolve.return_value = "job-1"
    service = MemoryCommandService(engine)

    assert asyncio.run(service.batch_write(items, continue_on_error=False)) is batch_result
    engine.batch_write.assert_awaited_once_with(items, continue_on_error=False)

    updated = asyncio.run(service.update("u1", _SCOPE, patch))
    assert updated.id == "u2"
    engine.update.assert_awaited_once_with("u1", _SCOPE, patch)

    deleted = asyncio.run(service.delete(selector))
    assert deleted == ["u1"]
    engine.delete.assert_awaited_once_with(selector)

    job_id = asyncio.run(service.evolve(_SCOPE, EvolveMode.EXTRACT, Channel.HOT))
    assert job_id == "job-1"
    engine.evolve.assert_awaited_once_with(_SCOPE, EvolveMode.EXTRACT, Channel.HOT)


def test_command_batch_write_aligned_restores_caller_index_and_item() -> None:
    engine = AsyncMock()
    marked = BatchWriteItem(content="a", scope=_SCOPE, system_metadata={"author": "x"})
    origin = BatchWriteItem(content="a", scope=_SCOPE)
    engine_outcome = BatchWriteOutcome(index=0, item=marked, units=[_unit()])
    engine.batch_write.return_value = BatchWriteResult(outcomes=[engine_outcome])
    service = MemoryCommandService(engine)

    remapped = asyncio.run(
        service.batch_write_aligned([marked], [(3, origin)], continue_on_error=True)
    )

    engine.batch_write.assert_awaited_once_with([marked], continue_on_error=True)
    assert remapped[0].index == 3
    assert remapped[0].item is origin
    collected = service.collect_batch_result({0: remapped[0]}, 1)
    assert collected.outcomes == [remapped[0]]


def test_query_recall_and_list_delegate_to_engine() -> None:
    engine = AsyncMock()
    query = RetrievalQuery(text="q")
    recall_result = RetrievalResult()
    engine.recall.return_value = recall_result
    listed = MemoryListResult(items=[], count=0)
    contexts = [PermissionContext(unit_id="u1", scope=_SCOPE)]
    engine.list_with_permission_contexts.return_value = (listed, contexts)
    engine.get.return_value = _unit()
    engine.permission_context_for_unit.return_value = contexts[0]
    engine.permission_contexts_for_delete.return_value = contexts
    service = MemoryQueryService(engine)

    assert asyncio.run(service.recall(_SCOPE, query)) is recall_result
    engine.recall.assert_awaited_once_with(_SCOPE, query)

    got_list, got_contexts = asyncio.run(
        service.list_with_permission_contexts(
            _SCOPE,
            offset=2,
            limit=5,
            memory_types=["episode"],
            extensions={"a": "b"},
            filters=None,
        )
    )
    assert got_list is listed
    assert got_contexts is contexts
    engine.list_with_permission_contexts.assert_awaited_once_with(
        _SCOPE,
        offset=2,
        limit=5,
        memory_types=["episode"],
        extensions={"a": "b"},
        filters=None,
    )

    assert asyncio.run(service.get("u1", _SCOPE, _NOW)) == _unit()
    engine.get.assert_awaited_once_with("u1", _SCOPE, _NOW)

    assert asyncio.run(service.permission_context_for_unit("u1", _SCOPE)) is contexts[0]
    selector = DeleteSelector(unit_ids=["u1"], scope=_SCOPE)
    assert asyncio.run(service.permission_contexts_for_delete(selector)) is contexts
    engine.permission_contexts_for_delete.assert_awaited_once_with(selector)


def test_space_lifecycle_purges_then_deletes_and_merges_counts() -> None:
    engine = AsyncMock()
    engine.purge_space.return_value = ["u1", "u2"]
    space = MagicMock()
    space.delete.return_value = SpaceDeleteResult(
        org="acme",
        space="coding",
        deleted_counts={"kv": 4},
        status=SpaceStatus.DELETED,
    )
    service = SpaceLifecycleService(engine, space)

    result, purged = asyncio.run(service.delete_space("acme", "coding"))

    space.begin_delete.assert_called_once_with("acme", "coding")
    engine.purge_space.assert_awaited_once_with("acme", "coding")
    space.delete.assert_called_once_with("acme", "coding")
    assert space.delete.call_args_list == [call("acme", "coding")]
    assert purged == ["u1", "u2"]
    assert result.org == "acme"
    assert result.space == "coding"
    assert result.deleted_counts["memory"] == 2
    assert result.deleted_counts["index"] == 2
    assert result.deleted_counts["kv"] == 6


def test_space_lifecycle_skips_space_delete_when_purge_fails() -> None:
    engine = AsyncMock()
    engine.purge_space.side_effect = RuntimeError("purge failed")
    space = MagicMock()
    service = SpaceLifecycleService(engine, space)

    with pytest.raises(RuntimeError, match="purge failed"):
        asyncio.run(service.delete_space("acme", "coding"))
    space.begin_delete.assert_called_once_with("acme", "coding")
    space.delete.assert_not_called()


def test_space_lifecycle_reports_partial_failure_when_delete_fails_after_purge() -> None:
    engine = AsyncMock()
    engine.purge_space.return_value = ["u1"]
    space = MagicMock()
    space.delete.side_effect = RuntimeError("metadata delete failed")
    service = SpaceLifecycleService(engine, space)

    with pytest.raises(PartialFailureError) as exc_info:
        asyncio.run(service.delete_space("acme", "coding"))

    err = exc_info.value
    assert err.completed == ("purge_space",)
    assert err.failed == "space.delete"
    assert err.retry_action == "delete_space"
    assert "retry delete_space" in str(err)
    space.begin_delete.assert_called_once_with("acme", "coding")
    engine.purge_space.assert_awaited_once_with("acme", "coding")
    space.delete.assert_called_once_with("acme", "coding")


def test_space_lifecycle_retry_completes_after_partial_metadata_delete_failure() -> None:
    engine = AsyncMock()
    engine.purge_space.side_effect = [["u1"], []]
    space = MagicMock()
    space.delete.side_effect = [
        RuntimeError("metadata delete failed"),
        SpaceDeleteResult(
            org="acme",
            space="coding",
            deleted_counts={"kv": 1},
            status=SpaceStatus.DELETED,
        ),
    ]
    service = SpaceLifecycleService(engine, space)

    with pytest.raises(PartialFailureError):
        asyncio.run(service.delete_space("acme", "coding"))
    result, purged = asyncio.run(service.delete_space("acme", "coding"))

    assert purged == []
    assert result.deleted_counts["kv"] == 1
    assert engine.purge_space.await_count == 2
    assert space.delete.call_count == 2


def test_governance_delegates_inspect_trace_audit() -> None:
    governor = MagicMock()
    units = [_unit()]
    governor.inspect.return_value = units
    governor.trace.return_value = units
    governor.audit.return_value = []
    service = GovernanceService(governor)

    assert service.inspect(["u1"], _SCOPE) is units
    governor.inspect.assert_called_once_with(["u1"], _SCOPE)
    assert service.trace("u1", _SCOPE) is units
    governor.trace.assert_called_once_with("u1", _SCOPE)
    assert service.audit({"actor": "alice"}, limit=10) == []
    governor.audit.assert_called_once_with({"actor": "alice"}, 10)
