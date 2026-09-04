# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.type_def import Segment
from jiuwen_memory_entry.core.dispatch_request import DispatchRequest
from jiuwen_memory_entry.core.legacy_request_adapter import build_legacy_dispatch_request

pytestmark = pytest.mark.unit

_BOOTSTRAP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "jiuwen_memory_entry",
    "core",
)
if _BOOTSTRAP not in sys.path:
    sys.path.append(_BOOTSTRAP)

handler = importlib.import_module("handler")


def _dispatch(srv, verb: str, payload: dict, *, identity=None):
    request = build_legacy_dispatch_request(verb, payload)
    if identity is not None:
        request = replace(request, actor=identity)
    return handler.dispatch(srv, request)


class _RecordingApi:
    def __init__(self) -> None:
        self.add_calls = []
        self.search_calls = []

    def add(
        self,
        content,
        scope,
        modality,
        *,
        security,
        tags=None,
        assets=None,
        system_metadata=None,
        user_metadata=None,
        occurred_at=None,
    ):
        self.add_calls.append(
            {"scope": scope, "identity": security.auth.actor, "modality": modality, "occurred_at": occurred_at}
        )
        return [handler.MemoryUnit(id="unit-1", scope=scope, segments=[Segment(content=content)])]

    def search(self, query, context, *, security, filters=None, **options):
        self.search_calls.append(
            {
                "query": query,
                "context": context,
                "identity": security.auth.actor,
                "filters": filters,
                "options": options,
            }
        )
        return SimpleNamespace(items=[], trajectory=[])


class _RecordingServer:
    def __init__(self) -> None:
        self.api = _RecordingApi()


def _dispatch_add(payload: dict) -> dict:
    srv = _RecordingServer()
    status, body = _dispatch(srv, "add", {"content": "hello", **payload})

    assert status == 200, body
    return srv.api.add_calls[0]


def test_add_forwards_occurred_at_to_api() -> None:
    from datetime import datetime

    call = _dispatch_add({"occurred_at": "2026-08-26T12:00:00"})
    assert call["occurred_at"] == datetime.fromisoformat("2026-08-26T12:00:00")


def test_add_invalid_occurred_at_returns_400() -> None:
    srv = _RecordingServer()
    status, body = _dispatch(
        srv, "add", {"content": "hello", "occurred_at": "not-a-date"},
    )
    assert status == 400
    assert body["error"] == "ValidationError"


def test_actor_scope_and_target_scope_match_when_actor_fields_are_omitted() -> None:
    call = _dispatch_add({"tenant_id": "acme", "space": "product", "scope": "alice"})

    assert call["identity"] == call["scope"]
    assert call["identity"].org == "acme"
    assert call["identity"].space == "product"
    assert call["identity"].user == "alice"


def test_single_add_uses_source_as_the_canonical_modality_field() -> None:
    call = _dispatch_add({"source": "image"})

    assert call["modality"] is handler.Modality.IMAGE


def test_actor_scope_uses_default_scope_when_identity_fields_are_omitted() -> None:
    call = _dispatch_add({})

    assert call["identity"] == handler.Scope(org="default", user="")
    assert call["scope"] == handler.Scope(org="default", user="")


def test_actor_scope_override_inherits_target_tenant_when_actor_tenant_not_provided() -> None:
    call = _dispatch_add(
        {
            "tenant_id": "acme",
            "space_id": "product",
            "scope": "owner",
            "actor_scope": "auditor",
        }
    )

    assert call["identity"] == handler.Scope(org="acme", space="product", user="auditor")
    assert call["scope"] == handler.Scope(org="acme", space="product", user="owner")


def test_search_forwards_filter_dsl_to_api_boundary() -> None:
    srv = _RecordingServer()
    filters = {
        "AND": [
            {"metadata.memory_type": "coding"},
            {"OR": [{"project": "alpha"}, {"project": "beta"}]},
        ]
    }

    status, body = _dispatch(
        srv,
        "search",
        {"query": "pytest", "tenant_id": "acme", "scope": "alice", "filters": filters},
    )

    assert status == 200, body
    assert srv.api.search_calls[0]["filters"] == filters


def test_search_preserves_json_extensions_and_returns_both_metadata_namespaces() -> None:
    class _Api:
        def __init__(self) -> None:
            self.context = None

        def search(self, query, context, *, security, filters=None, **options):
            del query, security, filters, options
            self.context = context
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        score=0.8,
                        unit_id="unit-1",
                        content="remembered content",
                        system_metadata={"memory_type": "coding"},
                        user_metadata={"project": "alpha"},
                    )
                ],
                trajectory=[],
            )

    api = _Api()
    status, body = _dispatch(
        SimpleNamespace(api=api),
        "search",
        {
            "query": "remember",
            "extensions": {"routing": {"mode": "strict"}, "attempt": 2},
        },
    )

    assert status == 200, body
    assert api.context.extensions == {"routing": {"mode": "strict"}, "attempt": 2}
    assert body["hits"] == [
        {
            "score": 0.8,
            "item_id": "unit-1",
            "content": "remembered content",
            "system_metadata": {"memory_type": "coding"},
            "user_metadata": {"project": "alpha"},
        }
    ]


def test_five_dimension_payload_maps_onto_scope() -> None:
    call = _dispatch_add(
        {
            "tenant_id": "acme",
            "space": "product",
            "scope": "alice",
            "agent": "bot",
            "session": "sess-1",
        }
    )

    expected = handler.Scope(
        org="acme", space="product", user="alice", agent="bot", session="sess-1"
    )
    assert call["scope"] == expected
    assert call["identity"] == expected


def test_same_org_different_space_stay_isolated() -> None:
    from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel

    params = {
        "ingestor": "default",
        "index_builder": "default",
        "retriever": "default",
        "kv_store": "default",
        "scheduler": "default",
        "evolver": "default",
        "lifecycle": "default",
    }

    class _KernelServer:
        def __init__(self) -> None:
            self.api = build_kernel(
                config={"engine": {"default": {"target": "cloud", "params": params}}}
            ).api

    srv = _KernelServer()
    payload_a = {
        "content": "alpha-only",
        "tenant_id": "acme",
        "space": "alpha",
        "scope": "user",
    }
    payload_b = {
        "content": "beta-only",
        "tenant_id": "acme",
        "space": "beta",
        "scope": "user",
    }
    status, body = _dispatch(srv, "add", payload_a)
    assert status == 200, body
    status, body = _dispatch(srv, "add", payload_b)
    assert status == 200, body

    status, listed = _dispatch(
        srv, "list", {"tenant_id": "acme", "space": "alpha", "scope": "user"}
    )
    assert status == 200, listed
    contents = [item["content"] for item in listed["items"]]
    assert "alpha-only" in contents, listed
    assert "beta-only" not in contents, listed


def test_actor_space_override_can_differ_from_target_space() -> None:
    call = _dispatch_add(
        {
            "tenant_id": "acme",
            "space": "product",
            "scope": "owner",
            "actor_space": "coding",
            "actor_scope": "reader",
        }
    )

    assert call["identity"] == handler.Scope(org="acme", space="coding", user="reader")
    assert call["scope"] == handler.Scope(org="acme", space="product", user="owner")


def test_explicit_adapter_identity_wins_over_payload_identity_claims() -> None:
    srv = _RecordingServer()
    trusted = handler.Scope(org="acme", user="trusted")
    status, body = _dispatch(
        srv,
        "add",
        {
            "content": "hello",
            "tenant_id": "acme",
            "scope": "owner",
            "actor_scope": "forged",
        },
        identity=trusted,
    )

    assert status == 200, body
    assert srv.api.add_calls[0]["identity"] == trusted


def test_structured_request_uses_typed_actor_and_target_not_payload_claims() -> None:
    srv = _RecordingServer()
    actor = handler.Scope(org="acme", user="writer")
    target = handler.Scope(org="acme", space="product", user="alice")

    status, body = handler.dispatch(
        srv,
        DispatchRequest(
            verb="add",
            actor=actor,
            target=target,
            payload={
                "content": "hello",
                "tenant_id": "forged-org",
                "scope": "forged-target",
                "actor_scope": "forged-actor",
            },
        ),
    )

    assert status == 200, body
    assert srv.api.add_calls[0] == {
        "scope": target,
        "identity": actor,
        "modality": handler.Modality.TEXT,
        "occurred_at": None,
    }


def test_legacy_adapter_preserves_actor_fallback_without_leaking_scope_fields() -> None:
    request = build_legacy_dispatch_request(
        "add",
        {
            "tenant_id": "acme",
            "space": "product",
            "scope": "owner",
            "actor_scope": "writer",
            "content": "hello",
        },
    )

    assert request.target == handler.Scope(org="acme", space="product", user="owner")
    assert request.actor == handler.Scope(org="acme", space="product", user="writer")
    assert dict(request.payload) == {"content": "hello"}
