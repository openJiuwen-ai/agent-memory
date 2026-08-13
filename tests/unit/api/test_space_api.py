from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.common.errors import NotFoundError, PermissionDeniedError, ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import Config
from jiuwen_memory.control import PrincipalPath, SpaceMember, SpacePolicy, SpaceSpec, SpaceStatus

pytestmark = pytest.mark.unit


def _cloud_kernel():
    params = {}
    component_names = (
        "ingestor",
        "index_builder",
        "retriever",
        "kv_store",
        "scheduler",
        "evolver",
        "lifecycle",
    )
    for name in component_names:
        params[name] = "default"
    return build_kernel(
        config=Config.from_dict(
            {"engine": {"default": {"target": "cloud", "params": params}}}
        )
    )


def test_memory_api_space_lifecycle_usage_members_and_delete() -> None:
    kernel = _cloud_kernel()
    api = kernel.api
    org_admin = Scope(org="acme")
    space_admin = Scope(org="acme", space="coding")
    unit_scope = Scope(org="acme", space="coding", user="alice")

    info = api.create_space(
        SpaceSpec(org="acme", space="coding", display_name="Coding"),
        identity=org_admin,
    )
    assert info.status == SpaceStatus.ACTIVE
    assert api.get_space("acme", "coding", identity=space_admin).display_name == "Coding"
    assert [space.space for space in api.list_spaces("acme", identity=org_admin)] == ["coding"]

    api.add_space_member(
        "acme",
        "coding",
        SpaceMember(scope=Scope(user="alice"), role="admin"),
        identity=space_admin,
    )
    assert api.list_space_members("acme", "coding", identity=space_admin)[0].scope == unit_scope

    unit = api.add("space scoped memory", unit_scope, identity=unit_scope)[0]
    usage = api.space_usage("acme", "coding", identity=space_admin)
    assert usage.memory_count == 1
    assert usage.storage_bytes > 0
    assert api.export_space("acme", "coding", identity=space_admin)

    result = api.delete_space("acme", "coding", identity=space_admin)
    assert result.deleted_counts["memory"] == 1
    assert result.deleted_counts["index"] == 1
    events = api.audit({"target_space": "coding"}, identity=Scope(), limit=100)
    assert unit.id in events[-1].detail.get("deleted_memory_ids", "")
    with pytest.raises(NotFoundError):
        api.get_space("acme", "coding", identity=space_admin)


def test_memory_api_rejects_writes_after_space_archive() -> None:
    api = _cloud_kernel().api
    org_admin = Scope(org="acme")
    space_admin = Scope(org="acme", space="coding")
    unit_scope = Scope(org="acme", space="coding", user="alice")

    api.create_space(SpaceSpec(org="acme", space="coding"), identity=org_admin)
    api.archive_space("acme", "coding", identity=space_admin)

    with pytest.raises(ValidationError):
        api.add("blocked after archive", unit_scope, identity=unit_scope)


def test_space_policy_principal_path_drives_api_authorization() -> None:
    api = _cloud_kernel().api
    org_admin = Scope(org="acme")
    target = Scope(org="acme", space="coding", agent="agent-a", user="alice")

    api.create_space(
        SpaceSpec(
            org="acme",
            space="coding",
            principal_path=PrincipalPath.AGENT_USER,
            policy=SpacePolicy(principal_path=PrincipalPath.AGENT_USER),
        ),
        identity=org_admin,
    )

    api.add(
        "agent owns user memory in this space",
        target,
        identity=Scope(org="acme", space="coding", agent="agent-a"),
    )
    with pytest.raises(PermissionDeniedError):
        api.add(
            "user is not the parent in this space",
            target,
            identity=Scope(org="acme", space="coding", user="alice"),
        )


def test_in_memory_engine_rejects_non_empty_space() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", space="cloud-space", user="alice")

    with pytest.raises(ValidationError, match="InMemoryEngine"):
        api.add("cloud scoped memory", scope, identity=scope)
