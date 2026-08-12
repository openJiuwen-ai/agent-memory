from __future__ import annotations

import asyncio

import pytest

from api.memory_api_impl import build_kernel
from common.type_def import Modality, Scope
from config import Config
from control import BatchWriteItem

pytestmark = pytest.mark.unit


def test_batch_add_normalizes_defaults_and_preserves_input_order() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="alice")

    result = api.batch_add(
        [
            BatchWriteItem(content="first", tags=["shared", "first"], sequence=1),
            BatchWriteItem(
                content="second",
                source=Modality.CODE,
                tags=["shared", "second"],
                metadata={"priority": 2},
                sequence=2,
            ),
        ],
        scope,
        identity=scope,
        tags=["shared"],
        metadata={"project": "batch", "priority": 1},
        stream_id="import-1",
    )

    assert [outcome.index for outcome in result.outcomes] == [0, 1]
    assert all(not outcome.error for outcome in result.outcomes)
    assert result.outcomes[0].item.tags == ["shared", "first"]
    assert result.outcomes[1].item.tags == ["shared", "second"]
    assert result.outcomes[1].item.metadata == {"project": "batch", "priority": 2}
    assert result.outcomes[1].item.source == Modality.CODE


def test_batch_add_collects_item_validation_errors_and_continues() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="alice")

    result = api.batch_add(
        [
            BatchWriteItem(content=None),  # type: ignore[arg-type]
            BatchWriteItem(content="valid"),
        ],
        scope,
        identity=scope,
    )

    assert result.outcomes[0].error_type == "ValidationError"
    assert result.outcomes[1].error == ""
    assert result.outcomes[1].units[0].content == "valid"


def test_batch_add_fail_fast_marks_remaining_items_skipped() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="alice")

    result = api.batch_add(
        [
            BatchWriteItem(content=None),  # type: ignore[arg-type]
            BatchWriteItem(content="not-written"),
        ],
        scope,
        identity=scope,
        continue_on_error=False,
    )

    assert result.outcomes[0].error_type == "ValidationError"
    assert result.outcomes[1].error_type == "Skipped"


def test_batch_add_rejects_duplicate_sequence_within_scope_and_stream() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="alice")

    result = api.batch_add(
        [BatchWriteItem(content="first", sequence=1), BatchWriteItem(content="second", sequence=1)],
        scope,
        identity=scope,
        stream_id="import-1",
    )

    assert result.outcomes[0].error == ""
    assert result.outcomes[1].error_type == "ValidationError"


def test_batch_add_authorizes_each_item_without_blocking_later_owner_item() -> None:
    api = build_kernel(config=Config.from_dict({"permission": {"default": "sqlite"}})).api
    owner = Scope(org="acme", user="owner")
    reader = Scope(org="acme", user="reader")

    result = api.batch_add(
        [
            BatchWriteItem(content="denied", scope=owner),
            BatchWriteItem(content="allowed", scope=reader),
        ],
        identity=reader,
    )

    assert result.outcomes[0].error_type == "PermissionDeniedError"
    assert result.outcomes[1].units[0].content == "allowed"


def test_batch_add_async_matches_synchronous_result_shape() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="alice")

    result = asyncio.run(
        api.batch_add_async([BatchWriteItem(content="async")], scope, identity=scope)
    )

    assert len(result.outcomes) == 1
    assert result.outcomes[0].units[0].content == "async"
