from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import NotFoundError, PermissionDeniedError, ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import Config
from jiuwen_memory.control import PrincipalPath, SpaceMember, SpacePolicy, SpaceSpec, SpaceStatus
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

# 平台运维主体：查跨 org 审计用。必须具名且带 ROOT——空 Scope 在 PR2 已不是特权形态
# （判定实现第 2 步 ``empty_actor`` 直接拒），而不带 org 的审计查询是系统级资源，
# 角色闸门只认 ROOT。
SEC_PLATFORM_OPS = internal_context(
    ScopedAuthenticator(Scope(org="system", user="platform-ops"), role=Role.ROOT)
)


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
    # 保留旧配置兼容输入，但生产判定统一走真实 StandardAuthorizer，未启用 allow_all。
    return build_kernel(
        config=Config.from_dict(
            {
                "engine": {"default": {"target": "cloud", "params": params}},
                "permission": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}},
            }
        )
    )


def test_memory_api_space_lifecycle_usage_members_and_delete() -> None:
    kernel = _cloud_kernel()
    api = kernel.api
    org_admin = Scope(org="acme", user="org-admin")
    unit_scope = Scope(org="acme", space="coding", user="alice")

    info = api.create_space(
        SpaceSpec(org="acme", space="coding", display_name="Coding"),
        security=internal_context(ScopedAuthenticator(org_admin, role=Role.ADMIN)),
    )
    assert info.status == SpaceStatus.ACTIVE
    assert api.get_space("acme", "coding", security=SEC_PLATFORM_OPS).display_name == "Coding"
    spaces = api.list_spaces("acme", security=SEC_PLATFORM_OPS)
    assert [space.space for space in spaces] == ["coding"]

    api.add_space_member(
        "acme",
        "coding",
        SpaceMember(scope=Scope(user="alice"), role="admin"),
        security=SEC_PLATFORM_OPS,
    )
    assert (
        api.list_space_members("acme", "coding", security=SEC_PLATFORM_OPS)[0].scope == unit_scope
    )

    unit = api.add(
        "space scoped memory",
        unit_scope,
        security=internal_context(ScopedAuthenticator(unit_scope)),
    )[0]
    usage = api.space_usage("acme", "coding", security=SEC_PLATFORM_OPS)
    assert usage.memory_count == 1
    assert usage.storage_bytes > 0
    assert api.export_space("acme", "coding", security=SEC_PLATFORM_OPS)

    result = api.delete_space("acme", "coding", security=SEC_PLATFORM_OPS)
    assert result.deleted_counts["memory"] == 1
    assert result.deleted_counts["index"] == 1
    events = api.audit({"target_space": "coding"}, security=SEC_PLATFORM_OPS, limit=100)
    assert unit.id in events[-1].detail.get("deleted_memory_ids", "")
    with pytest.raises(NotFoundError):
        api.get_space("acme", "coding", security=SEC_PLATFORM_OPS)


def test_memory_api_rejects_writes_after_space_archive() -> None:
    api = _cloud_kernel().api
    org_admin = Scope(org="acme", user="org-admin")
    unit_scope = Scope(org="acme", space="coding", user="alice")

    api.create_space(
        SpaceSpec(org="acme", space="coding"),
        security=internal_context(ScopedAuthenticator(org_admin, role=Role.ADMIN)),
    )
    api.archive_space("acme", "coding", security=SEC_PLATFORM_OPS)

    with pytest.raises(ValidationError):
        api.add(
            "blocked after archive",
            unit_scope,
            security=internal_context(ScopedAuthenticator(unit_scope)),
        )


def test_space_policy_principal_path_drives_api_authorization() -> None:
    api = _cloud_kernel().api
    org_admin = Scope(org="acme", user="org-admin")
    target = Scope(org="acme", space="coding", agent="agent-a", user="alice")

    api.create_space(
        SpaceSpec(
            org="acme",
            space="coding",
            principal_path=PrincipalPath.AGENT_USER,
            policy=SpacePolicy(principal_path=PrincipalPath.AGENT_USER),
        ),
        security=internal_context(ScopedAuthenticator(org_admin, role=Role.ADMIN)),
    )

    api.add(
        "agent owns user memory in this space",
        target,
        security=internal_context(
            ScopedAuthenticator(Scope(org="acme", space="coding", agent="agent-a"))
        ),
    )
    with pytest.raises(PermissionDeniedError):
        api.add(
            "user is not the parent in this space",
            target,
            security=internal_context(
                ScopedAuthenticator(Scope(org="acme", space="coding", user="alice"))
            ),
        )


def test_deleting_space_blocks_writes_but_allows_delete_space() -> None:
    kernel = _cloud_kernel()
    api = kernel.api
    org_admin = Scope(org="acme", user="org-admin")
    unit_scope = Scope(org="acme", space="lab", user="alice")
    api.create_space(
        SpaceSpec(org="acme", space="lab"),
        security=internal_context(ScopedAuthenticator(org_admin, role=Role.ADMIN)),
    )
    api.add("before delete", unit_scope, security=internal_context(ScopedAuthenticator(unit_scope)))
    kernel.space.begin_delete("acme", "lab")
    with pytest.raises(ValidationError, match="deleting"):
        api.add(
            "after delete started",
            unit_scope,
            security=internal_context(ScopedAuthenticator(unit_scope)),
        )
    result = api.delete_space("acme", "lab", security=SEC_PLATFORM_OPS)
    assert result.status is SpaceStatus.DELETED


def test_in_memory_engine_rejects_non_empty_space() -> None:
    api = build_kernel().api
    scope = Scope(org="acme", space="cloud-space", user="alice")

    with pytest.raises(ValidationError, match="InMemoryEngine"):
        api.add("cloud scoped memory", scope, security=internal_context(ScopedAuthenticator(scope)))
