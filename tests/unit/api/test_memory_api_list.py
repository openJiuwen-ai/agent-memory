from __future__ import annotations

import pytest

from api.memory_api_impl import build_kernel
from common.errors import PermissionDeniedError, ValidationError
from common.type_def import (
    FilterClause,
    FilterOp,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
    messages_key,
)
from common.type_def.memory_codec import dumps
from config import Config

pytestmark = pytest.mark.unit


def _routing_config() -> Config:
    return Config.from_dict(
        {
            "permission": {
                "default": {
                    "target": "routing",
                    "params": {
                        "route_key": "memory_type",
                        "fallback": "strict",
                        "routes": {"coding": "strict", "episodic": "standard"},
                    },
                },
                "standard": "allow_all",
                "strict": "sqlite",
            }
        }
    )


def test_memory_api_list_supports_pagination_and_memory_type_filter() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="owner")

    episodic = api.write(
        "alice joined the sprint planning",
        scope,
        identity=scope,
        metadata={"memory_type": "episodic"},
    )[0]
    coding = api.write(
        "repo uses pytest for unit tests",
        scope,
        identity=scope,
        metadata={"memory_type": "coding"},
    )[0]
    semantic = api.write(
        "alice prefers concise summaries",
        scope,
        identity=scope,
        metadata={"memory_type": "semantic"},
    )[0]

    coding_result = api.list(scope, identity=scope, memory_types=["coding"])
    all_result = api.list(scope, identity=scope)
    second_page = api.list(scope, identity=scope, offset=1, limit=1)

    assert [unit.id for unit in coding_result.items] == [coding.id]
    assert coding_result.count == 1
    assert len(second_page.items) == 1
    assert second_page.items[0].id == all_result.items[1].id
    assert second_page.count == 3
    assert {unit.id for unit in all_result.items} == {episodic.id, coding.id, semantic.id}


def test_memory_api_list_is_scope_bound_and_ignores_message_prefix_records() -> None:
    kernel = build_kernel()
    api = kernel.api
    owner = Scope(org="acme", user="owner")
    other = Scope(org="acme", user="other")

    visible = api.write("visible indexed memory", owner, identity=owner)[0]
    hidden = MemoryUnit(
        id="raw-message",
        scope=owner,
        segments=[Segment(content="hidden infer source")],
        temporal=Temporal(t_ingest=visible.temporal.t_ingest),
    )
    kernel.kv.insert(owner, messages_key(hidden.id), dumps(hidden))
    api.write("other tenant memory", other, identity=other)

    listed = api.list(owner, identity=owner)

    assert [unit.id for unit in listed.items] == [visible.id]
    assert listed.count == 1


def test_memory_api_list_filters_before_pagination_and_preserves_total_count() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="owner")

    first = api.write(
        "first alpha memory",
        scope,
        identity=scope,
        metadata={"memory_type": "coding", "project": "alpha", "priority": 1},
    )[0]
    second = api.write(
        "second alpha memory",
        scope,
        identity=scope,
        metadata={"memory_type": "coding", "project": "alpha", "priority": 2},
    )[0]
    api.write(
        "beta memory",
        scope,
        identity=scope,
        metadata={"memory_type": "coding", "project": "beta", "priority": 3},
    )

    result = api.list(
        scope,
        identity=scope,
        offset=1,
        limit=1,
        memory_types=["coding"],
        filters={
            "AND": [
                {"metadata.project": "alpha"},
                {"metadata.priority": {"gte": 1}},
            ]
        },
    )

    assert result.count == 2
    assert len(result.items) == 1
    assert result.items[0].id in {first.id, second.id}


def test_memory_api_list_copies_extensions_and_forwards_normalized_filters() -> None:
    kernel = build_kernel()
    api = kernel.api
    scope = Scope(org="acme", user="owner")
    api.write(
        "alpha memory",
        scope,
        identity=scope,
        metadata={"project": "alpha"},
    )
    extensions = {"vendor_mode": 7}
    calls = []
    original_list = kernel.kv.list

    def recording_list(target_scope, **kwargs):
        calls.append((target_scope, kwargs))
        return original_list(target_scope, **kwargs)

    kernel.kv.list = recording_list
    filters = FilterClause("metadata.project", FilterOp.EQ, "alpha")

    result = api.list(
        scope,
        identity=scope,
        extensions=extensions,
        filters=filters,
    )

    assert result.count == 1
    assert extensions == {"vendor_mode": 7}
    assert calls[0][0] == scope
    assert calls[0][1]["extensions"] == {"vendor_mode": "7"}
    assert calls[0][1]["extensions"] is not extensions
    assert calls[0][1]["filters"] == filters


def test_memory_api_list_rejects_invalid_extensions_and_scope_filter() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="owner")

    with pytest.raises(ValidationError):
        api.list(scope, identity=scope, extensions=["invalid"])
    with pytest.raises(ValidationError):
        api.list(scope, identity=scope, filters={"space": "other"})


def test_memory_api_list_validates_pagination() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", user="owner")

    with pytest.raises(ValidationError):
        api.list(scope, identity=scope, offset=-1)
    with pytest.raises(ValidationError):
        api.list(scope, identity=scope, limit=0)


def test_memory_api_list_permission_routes_by_memory_type() -> None:
    api = build_kernel(config=_routing_config()).api
    owner = Scope(org="acme", user="owner")
    reader = Scope(org="acme", user="reader")

    api.list(owner, identity=reader, memory_types=["episodic"])
    with pytest.raises(PermissionDeniedError):
        api.list(owner, identity=reader, memory_types=["coding"])
    with pytest.raises(PermissionDeniedError):
        api.list(owner, identity=reader, memory_types=["episodic", "coding"])


def test_memory_api_unfiltered_list_uses_strict_fallback() -> None:
    api = build_kernel(config=_routing_config()).api
    owner = Scope(org="acme", user="owner")
    reader = Scope(org="acme", user="reader")
    api.write(
        "private coding memory",
        owner,
        identity=owner,
        metadata={"memory_type": "coding"},
    )

    with pytest.raises(PermissionDeniedError):
        api.list(owner, identity=reader)


def test_memory_api_list_binds_extension_permission_route_to_filter() -> None:
    api = build_kernel(config=_routing_config()).api
    owner = Scope(org="acme", user="owner")
    reader = Scope(org="acme", user="reader")
    episodic = api.write(
        "shareable episodic memory",
        owner,
        identity=owner,
        metadata={"memory_type": "episodic"},
    )[0]
    api.write(
        "private coding memory",
        owner,
        identity=owner,
        metadata={"memory_type": "coding"},
    )

    result = api.list(
        owner,
        identity=reader,
        extensions={"memory_type": "episodic"},
    )

    assert result.count == 1
    assert [unit.id for unit in result.items] == [episodic.id]
