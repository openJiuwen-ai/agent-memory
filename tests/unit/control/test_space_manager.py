from __future__ import annotations

import pytest

from common.errors import ConflictError, NotFoundError, ValidationError
from common.type_def import Scope
from control import PrincipalPath, SpaceMember, SpacePatch, SpacePolicy, SpaceSpec, SpaceStatus
from control.space_impl.kv_space_manager import KVSpaceManager
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit


def test_kv_space_manager_crud_policy_members_usage_and_delete() -> None:
    kv = InMemoryKVStore()
    manager = KVSpaceManager(kv)

    info = manager.create(
        SpaceSpec(
            org="acme",
            space="coding",
            display_name="Coding",
            principal_path=PrincipalPath.AGENT_USER,
            policy=SpacePolicy(
                principal_path=PrincipalPath.AGENT_USER,
                pipeline_profiles={"coding": "coding"},
            ),
            metadata={"env": "prod"},
        )
    )

    assert info.status == SpaceStatus.ACTIVE
    assert manager.get("acme", "coding").policy.principal_path == PrincipalPath.AGENT_USER
    assert manager.list("acme") == [info]

    updated = manager.update(
        "acme",
        "coding",
        SpacePatch(display_name="Coding Memory", metadata={"team": "platform"}),
    )
    assert updated.display_name == "Coding Memory"
    assert updated.metadata["env"] == "prod"
    assert updated.metadata["team"] == "platform"

    member = SpaceMember(scope=Scope(agent="agent-a", user="alice"), role="admin")
    manager.add_member("acme", "coding", member)
    members = manager.list_members("acme", "coding")
    assert len(members) == 1
    assert members[0].scope == Scope(org="acme", space="coding", agent="agent-a", user="alice")
    assert members[0].role == "admin"

    kv.insert(Scope(org="acme", space="coding", user="alice"), "/memory/u1", b"memory")
    kv.insert(Scope(org="acme", space="coding", user="alice"), "/messages/m1", b"message")
    usage = manager.usage("acme", "coding")
    assert usage.memory_count == 1
    assert usage.message_count == 1
    assert usage.storage_bytes > 0

    result = manager.delete("acme", "coding")
    assert result.status == SpaceStatus.DELETED
    assert result.deleted_counts["kv"] >= 4
    assert not kv.list(Scope(org="acme", space="coding"))
    assert not kv.list(Scope(org="acme", space="coding", user="alice"))


def test_kv_space_manager_validates_and_reports_conflicts() -> None:
    manager = KVSpaceManager(InMemoryKVStore())

    with pytest.raises(ValidationError):
        manager.create(SpaceSpec(org="acme"))

    manager.create(SpaceSpec(org="acme", space="coding"))
    with pytest.raises(ConflictError):
        manager.create(SpaceSpec(org="acme", space="coding"))
    with pytest.raises(ConflictError):
        manager.create(SpaceSpec(org="other", space="coding"))
    with pytest.raises(NotFoundError):
        manager.get("acme", "unknown")
