from __future__ import annotations

import pytest

from api.memory_api_impl import build_kernel
from common.errors import NotFoundError, PermissionDeniedError, ValidationError
from common.security.types import Role
from common.type_def import Scope
from config import Config
from control import PrincipalPath, SpaceMember, SpacePolicy, SpaceSpec, SpaceStatus
from tests.conftest import root_sec, sec

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


def _admin(actor: Scope):
    """space 管理面（``MANAGE_SPACE``）的最低角色是 ADMIN。

    原先「管理员」是靠 actor 的 scope 形状表达的（``Scope(org="acme")`` 这种少填几维
    的形状恰好覆盖得更宽）；现在角色是 ``AuthContext`` 里的独立字段，形状只决定
    owner 覆盖范围、够不到管理面。两者都要给对，用例才测到真实的准入路径。
    """
    return sec(actor, role=Role.ADMIN)


def test_memory_api_space_lifecycle_usage_members_and_delete() -> None:
    kernel = _cloud_kernel()
    api = kernel.api
    org_admin = Scope(org="acme")
    space_admin = Scope(org="acme", space="coding")
    unit_scope = Scope(org="acme", space="coding", user="alice")

    info = api.create_space(
        SpaceSpec(org="acme", space="coding", display_name="Coding"),
        security=_admin(org_admin),
    )
    assert info.status == SpaceStatus.ACTIVE
    assert api.get_space("acme", "coding", security=sec(space_admin)).display_name == "Coding"
    assert [space.space for space in api.list_spaces("acme", security=sec(org_admin))] == ["coding"]

    api.add_space_member(
        "acme",
        "coding",
        SpaceMember(scope=Scope(user="alice"), role="admin"),
        security=_admin(space_admin),
    )
    members = api.list_space_members("acme", "coding", security=sec(space_admin))
    assert members[0].scope == unit_scope

    unit = api.write("space scoped memory", unit_scope, security=sec(unit_scope))[0]
    usage = api.space_usage("acme", "coding", security=sec(space_admin))
    assert usage.memory_count == 1
    assert usage.storage_bytes > 0
    assert api.export_space("acme", "coding", security=sec(space_admin))

    result = api.delete_space("acme", "coding", security=_admin(space_admin))
    assert result.deleted_counts["memory"] == 1
    assert result.deleted_counts["index"] == 1
    events = api.audit({"target_space": "coding"}, security=root_sec(), limit=100)
    assert unit.id in events[-1].detail.get("deleted_memory_ids", "")
    with pytest.raises(NotFoundError):
        api.get_space("acme", "coding", security=sec(space_admin))


def test_space_management_requires_admin_role() -> None:
    """USER 角色够不到 space 管理面，哪怕它的 scope 覆盖那个 space。

    这条是上面那个 ``_admin`` 的反面：owner 覆盖只管数据面，管理面的准入依据是
    服务端签发的 role，调用方改不了自己的 scope 就提权。
    """
    api = _cloud_kernel().api

    with pytest.raises(PermissionDeniedError):
        api.create_space(SpaceSpec(org="acme", space="coding"), security=sec(Scope(org="acme")))


def test_memory_api_rejects_writes_after_space_archive() -> None:
    api = _cloud_kernel().api
    org_admin = Scope(org="acme")
    space_admin = Scope(org="acme", space="coding")
    unit_scope = Scope(org="acme", space="coding", user="alice")

    api.create_space(SpaceSpec(org="acme", space="coding"), security=_admin(org_admin))
    api.archive_space("acme", "coding", security=_admin(space_admin))

    with pytest.raises(ValidationError):
        api.write("blocked after archive", unit_scope, security=sec(unit_scope))


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
        security=_admin(org_admin),
    )

    api.write(
        "agent owns user memory in this space",
        target,
        security=sec(Scope(org="acme", space="coding", agent="agent-a")),
    )
    with pytest.raises(PermissionDeniedError):
        api.write(
            "user is not the parent in this space",
            target,
            security=sec(Scope(org="acme", space="coding", user="alice")),
        )


def test_in_memory_engine_rejects_non_empty_space() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", space="cloud-space", user="alice")

    with pytest.raises(ValidationError, match="InMemoryEngine"):
        api.write("cloud scoped memory", scope, security=sec(scope))
