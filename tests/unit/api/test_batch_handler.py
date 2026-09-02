from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from jiuwen_memory_entry.core import handler
from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.control import BatchWriteItem, BatchWriteOutcome, BatchWriteResult

pytestmark = pytest.mark.unit


@dataclass
class _RecordingBatchApi:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def batch_add(self, items, scope, source, **kwargs):
        self.calls.append({"items": items, "scope": scope, "source": source, **kwargs})
        return BatchWriteResult(
            outcomes=[
                BatchWriteOutcome(index=index, item=item)
                if isinstance(item, BatchWriteItem)
                else BatchWriteOutcome(
                    index=index,
                    item=BatchWriteItem(content=""),
                    error="batch item must be BatchWriteItem",
                    error_type="ValidationError",
                )
                for index, item in enumerate(items)
            ]
        )


class _Server:
    def __init__(self) -> None:
        self.api = _RecordingBatchApi()


def test_batch_add_maps_defaults_item_scope_and_actor() -> None:
    srv = _Server()

    status, body = handler.dispatch(
        srv,
        "batch_add",
        {
            "defaults": {
                "tenant_id": "acme",
                "space": "product",
                "scope": "alice",
                "system_metadata": {"infer": "true"},
                "stream_id": "session-1",
                "occurred_at": "2026-08-05T10:00:00+00:00",
            },
            "actor_scope": "writer",
            "items": [
                {"content": "first", "sequence": 1},
                {
                    "content": "second",
                    "target_scope": {"scope": "bob"},
                    "source": "code",
                    "sequence": 2,
                    "occurred_at": "2026-08-05T10:01:00+00:00",
                },
            ],
        },
    )

    assert status == 200, body
    assert body["ok"] is True
    assert [outcome["input"]["content"] for outcome in body["outcomes"]] == ["first", "second"]
    call = srv.api.calls[0]
    assert call["security"].auth.actor == handler.Scope(
        org="acme", space="product", user="writer"
    )
    assert call["items"][0].scope == handler.Scope(org="acme", space="product", user="alice")
    assert call["items"][1].scope == handler.Scope(org="acme", space="product", user="bob")
    assert call["items"][1].source == handler.Modality.CODE
    assert call["system_metadata"] == {"infer": "true"}
    assert call["occurred_at"] == datetime.fromisoformat("2026-08-05T10:00:00+00:00")
    assert call["items"][0].occurred_at is None
    assert call["items"][1].occurred_at == datetime.fromisoformat("2026-08-05T10:01:00+00:00")


def test_batch_add_null_item_tenant_inherits_default_scope() -> None:
    srv = _Server()

    status, body = handler.dispatch(
        srv,
        "batch_add",
        {
            "defaults": {"tenant_id": "acme", "scope": "alice"},
            "items": [{"content": "remember", "target_scope": {"tenant_id": None}}],
        },
    )

    assert status == 200, body
    assert srv.api.calls[0]["items"][0].scope == handler.Scope(org="acme", user="alice")


def test_batch_add_rejects_legacy_metadata_field() -> None:
    status, body = handler.dispatch(
        _Server(),
        "batch_add",
        {
            "defaults": {"tenant_id": "acme", "metadata": {"infer": "true"}},
            "items": [{"content": "remember"}],
        },
    )

    assert status == 400
    assert body["error"] == "ValidationError"


def test_batch_add_returns_structured_outcome_for_malformed_item() -> None:
    status, body = handler.dispatch(
        _Server(),
        "batch_add",
        {"defaults": {"tenant_id": "acme"}, "items": ["invalid"]},
    )

    assert status == 200, body
    assert body["ok"] is False
    assert body["outcomes"][0]["error_type"] == "ValidationError"


def test_batch_add_malformed_item_does_not_raise_internal_error() -> None:
    class _KernelServer:
        def __init__(self) -> None:
            self.api = build_kernel().api

    status, body = handler.dispatch(
        _KernelServer(),
        "batch_add",
        {
            "defaults": {"tenant_id": "acme", "scope": "alice"},
            "items": ["invalid", {"content": "valid"}],
        },
    )

    assert status == 200, body
    assert body["outcomes"][0]["error_type"] == "ValidationError"
    assert body["outcomes"][1]["ok"] is True


@pytest.mark.parametrize(
    "item",
    [
        {"content": "invalid modality", "source": "unknown"},
        {"content": "invalid time", "occurred_at": "not-a-datetime"},
    ],
)
def test_batch_add_invalid_item_fields_return_structured_outcome(item: dict[str, str]) -> None:
    class _KernelServer:
        def __init__(self) -> None:
            self.api = build_kernel().api

    status, body = handler.dispatch(
        _KernelServer(),
        "batch_add",
        {
            "defaults": {"tenant_id": "acme", "scope": "alice"},
            "items": [item, {"content": "valid"}],
        },
    )

    assert status == 200, body
    assert body["outcomes"][0]["error_type"] == "ValidationError"
    assert body["outcomes"][1]["ok"] is True


def test_batch_add_invalid_default_occurred_at_returns_validation_error() -> None:
    status, body = handler.dispatch(
        _Server(),
        "batch_add",
        {
            "defaults": {"tenant_id": "acme", "occurred_at": "not-a-datetime"},
            "items": [{"content": "remember"}],
        },
    )

    assert status == 400
    assert body["error"] == "ValidationError"
