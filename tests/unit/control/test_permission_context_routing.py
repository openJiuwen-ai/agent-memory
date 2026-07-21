from __future__ import annotations

import pytest

from api.memory_api_impl import build_kernel
from common.errors import PermissionDeniedError
from common.type_def import Context, Scope
from config import Config
from control.types import DeleteMode, DeleteSelector


def _routing_config() -> Config:
    return Config.from_dict(
        {
            "permission": {
                "default": {
                    "target": "routing",
                    "params": {
                        "route_key": "memory_type",
                        "fallback": "standard",
                        "routes": {"coding": "strict"},
                    },
                },
                "standard": "allow_all",
                "strict": "sqlite",
            }
        }
    )


def test_write_permission_routes_by_memory_type() -> None:
    api = build_kernel(config=_routing_config()).api
    actor = Scope(org="acme", user="reader")
    target = Scope(org="acme", user="owner")

    api.write("general note", target, identity=actor, metadata={"memory_type": "episodic"})

    with pytest.raises(PermissionDeniedError):
        api.write(
            "repo must use pytest",
            target,
            identity=actor,
            metadata={"memory_type": "coding"},
        )


def test_recall_permission_routes_by_context_extensions() -> None:
    api = build_kernel(config=_routing_config()).api
    actor = Scope(org="acme", user="reader")
    target = Scope(org="acme", user="owner")

    api.recall(
        "general",
        Context(scope=target, extensions={"memory_type": "episodic"}),
        identity=actor,
    )

    with pytest.raises(PermissionDeniedError):
        api.recall(
            "repo",
            Context(scope=target, extensions={"memory_type": "coding"}),
            identity=actor,
        )


def test_get_permission_uses_stored_memory_type_context() -> None:
    api = build_kernel(config=_routing_config()).api
    owner = Scope(org="acme", user="owner")
    reader = Scope(org="acme", user="reader")
    unit = api.write(
        "repo must use pytest",
        owner,
        identity=owner,
        metadata={"memory_type": "coding"},
    )[0]

    with pytest.raises(PermissionDeniedError):
        api.get(unit.id, owner, identity=reader)


def test_delete_permission_checks_each_matched_unit_context() -> None:
    api = build_kernel(config=_routing_config()).api
    owner = Scope(org="acme", user="owner")
    reader = Scope(org="acme", user="reader")
    unit = api.write(
        "repo must use pytest",
        owner,
        identity=owner,
        tags=["repo"],
        metadata={"memory_type": "coding"},
    )[0]

    with pytest.raises(PermissionDeniedError):
        api.delete(
            DeleteSelector(unit_ids=[unit.id], scope=owner, mode=DeleteMode.FORGET),
            identity=reader,
        )
