from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from api.memory_api_impl import build_kernel
from bootstrap.core import handler

pytestmark = pytest.mark.unit


@dataclass
class _RecordingBatchApi:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def batch_write(self, items, scope, source, **kwargs):
        self.calls.append({"items": items, "scope": scope, "source": source, **kwargs})
        return handler._control_types_module.BatchWriteResult(
            outcomes=[
                handler._control_types_module.BatchWriteOutcome(index=index, item=item)
                if isinstance(item, handler.BatchWriteItem)
                else handler._control_types_module.BatchWriteOutcome(
                    index=index,
                    item=handler.BatchWriteItem(content=""),
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
                "metadata": {"infer": "true"},
                "stream_id": "session-1",
            },
            "actor_scope": "writer",
            "items": [
                {"content": "first", "sequence": 1},
                {
                    "content": "second",
                    "target_scope": {"scope": "bob"},
                    "source": "code",
                    "sequence": 2,
                },
            ],
        },
    )

    assert status == 200, body
    assert body["ok"] is True
    assert [outcome["input"]["content"] for outcome in body["outcomes"]] == ["first", "second"]
    call = srv.api.calls[0]
    assert call["identity"] == handler.Scope(org="acme", space="product", user="writer")
    assert call["items"][0].scope == handler.Scope(org="acme", space="product", user="alice")
    assert call["items"][1].scope == handler.Scope(org="acme", space="product", user="bob")
    assert call["items"][1].source == handler.Modality.CODE


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
