from __future__ import annotations

import importlib
import os
import sys
from types import SimpleNamespace

from common.type_def import Segment

_BOOTSTRAP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "bootstrap",
    "core",
)
if _BOOTSTRAP not in sys.path:
    sys.path.append(_BOOTSTRAP)

handler = importlib.import_module("handler")


class _RecordingApi:
    def __init__(self) -> None:
        self.write_calls = []
        self.recall_calls = []

    def write(
        self,
        content,
        scope,
        modality,
        *,
        identity,
        tags=None,
        assets=None,
        metadata=None,
    ):
        self.write_calls.append({"scope": scope, "identity": identity})
        return [handler.MemoryUnit(id="unit-1", scope=scope, segments=[Segment(content=content)])]

    def recall(self, query, context, *, identity, filters=None, **options):
        self.recall_calls.append(
            {
                "query": query,
                "context": context,
                "identity": identity,
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
    status, body = handler.dispatch(srv, "add", {"content": "hello", **payload})

    assert status == 200, body
    return srv.api.write_calls[0]


def test_actor_scope_and_target_scope_match_when_actor_fields_are_omitted() -> None:
    call = _dispatch_add({"tenant_id": "acme", "scope": "alice"})

    assert call["identity"] == call["scope"]
    assert call["identity"].org == "acme"
    assert call["identity"].user == "alice"


def test_actor_scope_uses_default_scope_when_identity_fields_are_omitted() -> None:
    call = _dispatch_add({})

    assert call["identity"] == handler.Scope(org="default", user="")
    assert call["scope"] == handler.Scope(org="default", user="")


def test_actor_scope_override_inherits_target_tenant_when_actor_tenant_not_provided() -> None:
    call = _dispatch_add({"tenant_id": "acme", "scope": "owner", "actor_scope": "auditor"})

    assert call["identity"] == handler.Scope(org="acme", user="auditor")
    assert call["scope"] == handler.Scope(org="acme", user="owner")


def test_search_forwards_filter_dsl_to_api_boundary() -> None:
    srv = _RecordingServer()
    filters = {
        "AND": [
            {"metadata.memory_type": "coding"},
            {"OR": [{"project": "alpha"}, {"project": "beta"}]},
        ]
    }

    status, body = handler.dispatch(
        srv,
        "search",
        {"query": "pytest", "tenant_id": "acme", "scope": "alice", "filters": filters},
    )

    assert status == 200, body
    assert srv.api.recall_calls[0]["filters"] == filters
