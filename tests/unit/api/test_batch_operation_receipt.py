"""Batch-level operation receipts: one logical write, one ID, one receipt."""

from __future__ import annotations

import pytest

from jiuwen_memory.api import (
    BatchWriteItem,
    ConflictError,
    ContinuityWrite,
    Scope,
    ValidationError,
    assemble,
)
from jiuwen_memory.common.security import internal_context
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


def _api_and_scope():
    api = assemble(config={})
    scope = Scope(org="acme", user="owner")
    return api, scope, internal_context(ScopedAuthenticator(scope))


def _items(*contents: str) -> list[BatchWriteItem]:
    return [BatchWriteItem(content=content) for content in contents]


def _ids(result) -> list[list[str]]:
    return [[unit.id for unit in outcome.units] for outcome in result.outcomes]


def test_replay_returns_first_result_without_writing():
    api, scope, security = _api_and_scope()
    first = api.batch_add(_items("a", "b"), scope, security=security, operation_id="op-1")
    ids = _ids(first)
    replay = api.batch_add(_items("a", "b"), scope, security=security, operation_id="op-1")
    assert _ids(replay) == ids, "replay must hand back the first result"
    assert len(api.list(scope, security=security).items) == 2, "replay must not write again"
    # Every replay is an independent copy: mutating one must not poison the next.
    replay.outcomes.clear()
    again = api.batch_add(_items("a", "b"), scope, security=security, operation_id="op-1")
    assert _ids(again) == ids, "stored receipt must survive caller mutation of a replay"


def test_receipt_is_queryable_through_operation_receipt():
    api, scope, security = _api_and_scope()
    api.batch_add(_items("a", "b"), scope, security=security, operation_id="op-1")
    receipt = api.operation_receipt("op-1", scope, security=security)
    assert receipt is not None, "batch receipts share the single-item receipt table"
    assert sorted(output["unit_id"] for output in receipt.outputs) == sorted(
        unit.id
        for outcome in receipt.batch_result.outcomes
        for unit in outcome.units
    )


def test_different_items_conflict():
    api, scope, security = _api_and_scope()
    api.batch_add(_items("a"), scope, security=security, operation_id="op-1")
    with pytest.raises(ConflictError, match="different content"):
        api.batch_add(_items("other"), scope, security=security, operation_id="op-1")
    with pytest.raises(ConflictError, match="different content"):
        api.batch_add(_items("a", "extra"), scope, security=security, operation_id="op-1")


def test_operation_id_is_not_shared_between_single_and_batch():
    api, scope, security = _api_and_scope()
    api.add("a", scope, security=security, operation_id="op-1")
    with pytest.raises(ConflictError, match="single write"):
        api.batch_add(_items("a"), scope, security=security, operation_id="op-1")
    api.batch_add(_items("b"), scope, security=security, operation_id="op-2")
    with pytest.raises(ConflictError, match="batch write"):
        api.add("b", scope, security=security, operation_id="op-2")


def test_partial_failure_is_recorded_as_is():
    api, scope, security = _api_and_scope()
    items = [BatchWriteItem(content="ok"), BatchWriteItem(content="")]
    first = api.batch_add(items, scope, security=security, operation_id="op-1")
    assert [outcome.error != "" for outcome in first.outcomes] == [False, True]
    replay = api.batch_add(items, scope, security=security, operation_id="op-1")
    assert [outcome.error_type for outcome in replay.outcomes] == [
        outcome.error_type for outcome in first.outcomes
    ], "replay returns the recorded outcome; rejected items are not retried"
    assert len(api.list(scope, security=security).items) == 1


def test_continuity_items_reject_batch_operation_id():
    api, scope, security = _api_and_scope()
    item = BatchWriteItem(
        content="x",
        continuity=ContinuityWrite(task_id="t", binding_id="b", event_id="e"),
    )
    with pytest.raises(ValidationError, match="continuity"):
        api.batch_add([item], scope, security=security, operation_id="op-1")


def test_receipt_is_isolated_by_subject():
    api, scope, security = _api_and_scope()
    first = api.batch_add(_items("a"), scope, security=security, operation_id="op-1")
    other_scope = Scope(org="acme", user="intruder")
    other = internal_context(ScopedAuthenticator(other_scope))
    theirs = api.batch_add(_items("a"), other_scope, security=other, operation_id="op-1")
    assert _ids(theirs) != _ids(first), "another subject's op id must not replay this receipt"
