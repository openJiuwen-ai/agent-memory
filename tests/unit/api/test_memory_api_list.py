from __future__ import annotations

import pytest

from api.memory_api_impl import build_kernel
from common.errors import PermissionDeniedError, ValidationError
from common.type_def import MemoryUnit, Scope, Segment, Temporal, messages_key
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

    coding_units = api.list(scope, identity=scope, memory_types=["coding"])
    all_units = api.list(scope, identity=scope)
    second_page = api.list(scope, identity=scope, offset=1, limit=1)

    assert [unit.id for unit in coding_units] == [coding.id]
    assert len(second_page) == 1
    assert second_page[0].id == all_units[1].id
    assert {unit.id for unit in all_units} == {episodic.id, coding.id, semantic.id}


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

    assert [unit.id for unit in listed] == [visible.id]


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
