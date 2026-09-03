from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.control import (
    PrincipalPath,
    SpaceMember,
    SpacePatch,
    SpacePolicy,
    SpaceSpec,
    SpaceStatus,
)
from jiuwen_memory.control.membership_impl.kv_membership_resolver import KVMembershipResolver
from jiuwen_memory.control.space_impl.kv_space_manager import KVSpaceManager
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.raw import KVRawDataStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage

pytestmark = pytest.mark.unit


def _acme_coding_values(kv_store: InMemoryKVStore) -> list[bytes]:
    """收集 ``acme/coding`` 空间下所有 KV 值，供 usage 字节数比对。"""
    return [
        value
        for candidate_scope in kv_store.scopes()
        if candidate_scope.org == "acme" and candidate_scope.space == "coding"
        for _, value in kv_store.scan(candidate_scope)
    ]


def _manager() -> KVSpaceManager:
    return KVSpaceManager(CompositeStorage(kv=InMemoryKVStore()))


def test_kv_space_manager_crud_policy_members_usage_and_delete() -> None:
    kv = InMemoryKVStore()
    manager = KVSpaceManager(CompositeStorage(kv=kv))

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

    # 成员记录主体维取单维（F07 不变量 12）：user 与 agent 同时非空即拒绝
    with pytest.raises(ValidationError):
        manager.add_member(
            "acme", "coding", SpaceMember(scope=Scope(agent="agent-a", user="alice"))
        )

    member = SpaceMember(scope=Scope(user="alice"), role="admin")
    manager.add_member("acme", "coding", member)
    members = manager.list_members("acme", "coding")
    assert len(members) == 1
    assert members[0].scope == Scope(org="acme", space="coding", user="alice")
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
    assert not kv.scan(Scope(org="acme", space="coding"))
    assert not kv.scan(Scope(org="acme", space="coding", user="alice"))


def test_space_manager_uses_independent_raw_port_for_usage_and_delete() -> None:
    main_kv = InMemoryKVStore()
    raw_kv = InMemoryKVStore()
    storage = CompositeStorage(kv=main_kv, raw=KVRawDataStore(raw_kv))
    manager = KVSpaceManager(storage)
    scope = Scope(org="acme", space="coding", user="alice")
    other_scope = Scope(org="acme", space="other", user="alice")

    manager.create(SpaceSpec(org="acme", space="coding"))
    manager.create(SpaceSpec(org="acme", space="other"))
    main_kv.insert(scope, "/memory/u1", b"memory")
    raw_port = storage.raw_port()
    raw_port.append_raw(
        scope,
        [MemoryUnit(id="m1", scope=scope, segments=[Segment(content="message")])],
    )
    raw_port.append_raw(
        other_scope,
        [MemoryUnit(id="m2", scope=other_scope, segments=[Segment(content="keep")])],
    )

    usage = manager.usage("acme", "coding")
    assert usage.memory_count == 1
    assert usage.message_count == 1
    main_values = _acme_coding_values(main_kv)
    raw_values = _acme_coding_values(raw_kv)
    expected_bytes = sum(len(value) for value in main_values) + sum(
        len(value) for value in raw_values
    )
    assert usage.storage_bytes == expected_bytes

    manager.delete("acme", "coding")

    assert raw_kv.scan(scope) == []
    assert raw_kv.scan(other_scope)


def test_space_manager_does_not_double_count_shared_raw_bytes() -> None:
    kv = InMemoryKVStore()
    storage = CompositeStorage(kv=kv)
    manager = KVSpaceManager(storage)
    scope = Scope(org="acme", space="coding", user="alice")
    unit = MemoryUnit(id="m1", scope=scope, segments=[Segment(content="message")])

    kv.insert(scope, "/memory/u1", b"memory")
    storage.raw_port().append_raw(scope, [unit])

    usage = manager.usage("acme", "coding")
    expected_bytes = sum(len(value) for _, value in kv.scan(scope))

    assert usage.memory_count == 1
    assert usage.message_count == 1
    assert usage.storage_bytes == expected_bytes


def test_kv_space_manager_validates_and_reports_conflicts() -> None:
    kv = InMemoryKVStore()
    manager = KVSpaceManager(CompositeStorage(kv=kv))

    with pytest.raises(ValidationError):
        manager.create(SpaceSpec(org="acme"))

    manager.create(SpaceSpec(org="acme", space="coding"))
    with pytest.raises(ConflictError):
        manager.create(SpaceSpec(org="acme", space="coding"))
    with pytest.raises(ConflictError):
        manager.create(SpaceSpec(org="other", space="coding"))
    with pytest.raises(NotFoundError):
        manager.get("acme", "unknown")


# -- 主体反查（原 storage/space_index 的用例，随实现并入本层） ---------------- #


def _alice() -> Scope:
    return Scope(org="acme", user="alice")


def test_owner_registration_puts_the_space_in_the_owners_reverse_lookup() -> None:
    """建空间即登记归属，反查随之可见；重复建同名空间被拒，索引不重复。"""
    manager = _manager()
    manager.create(SpaceSpec(org="acme", space="u-alice", owner=_alice()))

    assert manager.spaces_for(_alice(), "acme") == ("u-alice",)


def test_spaces_for_merges_three_buckets_and_sorts() -> None:
    """三路合并：user 桶、agent 桶、组织通配桶；返回值按空间名字典序。"""
    manager = _manager()
    manager.create(SpaceSpec(org="acme", space="u-alice", owner=_alice()))
    manager.create(SpaceSpec(org="acme", space="a-a1", owner=Scope(org="acme", agent="a1")))
    manager.create(SpaceSpec(org="acme", space="org-all"))
    manager.add_member("acme", "org-all", SpaceMember(scope=Scope(org="acme")))
    manager.create(SpaceSpec(org="acme", space="u-bob", owner=Scope(org="acme", user="bob")))

    actor = Scope(org="acme", user="alice", agent="a1")
    assert manager.spaces_for(actor, "acme") == ("a-a1", "org-all", "u-alice")
    # 另一 org 的同名主体互不可见——org 编在桶键里
    assert manager.spaces_for(Scope(org="other", user="alice"), "other") == ()


def test_a_two_dimension_principal_is_rejected_by_the_reverse_lookup() -> None:
    """索引按单维主体组织：双维记录在两维上同时命中，「两维各自约束」的语义会消失。"""
    manager = _manager()
    manager.create(SpaceSpec(org="acme", space="team"))
    with pytest.raises(ValidationError):
        manager.add_member(
            "acme", "team", SpaceMember(scope=Scope(org="acme", user="alice", agent="a1"))
        )


def test_deleting_a_space_clears_every_reverse_lookup_entry_pointing_at_it() -> None:
    """删空间要清掉指向它的全部索引项，否则候选集里留下打不开的空间名。"""
    manager = _manager()
    manager.create(SpaceSpec(org="acme", space="u-alice", owner=_alice()))
    manager.create(SpaceSpec(org="acme", space="team", owner=_alice()))
    manager.add_member("acme", "team", SpaceMember(scope=Scope(org="acme", agent="a1")))
    assert manager.spaces_for(_alice(), "acme") == ("team", "u-alice")

    manager.delete("acme", "team")

    actor = Scope(org="acme", user="alice", agent="a1")
    assert manager.spaces_for(actor, "acme") == ("u-alice",)


def test_health_surfaces_the_kv_failure_that_the_reverse_lookup_also_rides_on() -> None:
    """索引与主数据同落一个 KV，`health` 因此是单点：KV 不可用即整体不可用。

    折叠前索引是独立依赖，`health` 须逐个探测；折叠后只剩这一条链
    （``resolver.health`` → ``space.health`` → ``kv.health``），本用例把它钉在测试里，
    避免日后有人以为「索引没被探到」而再加一条探测支路。
    """

    class _BrokenKV(InMemoryKVStore):
        def health(self) -> None:
            raise BackendError("kv", "unavailable")

    manager = KVSpaceManager(CompositeStorage(kv=_BrokenKV()))
    with pytest.raises(BackendError):
        manager.health()
    with pytest.raises(BackendError):
        KVMembershipResolver(manager).health()
