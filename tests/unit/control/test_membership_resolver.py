from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.security.space_roles import SpaceContentRole, SpaceGovernanceRole
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.membership_impl.kv_membership_resolver import KVMembershipResolver
from jiuwen_memory.control.space_impl.kv_space_manager import KVSpaceManager
from jiuwen_memory.control.types import SpaceMember, SpaceSpec
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage

pytestmark = pytest.mark.unit


def _manager() -> KVSpaceManager:
    kv = InMemoryKVStore()
    return KVSpaceManager(CompositeStorage(kv=kv))


def _resolver(manager: KVSpaceManager, **kwargs) -> KVMembershipResolver:
    return KVMembershipResolver(manager, **kwargs)


def test_facts_reports_individual_space_and_filters_expired_members() -> None:
    manager = _manager()
    manager.create(SpaceSpec(org="acme", space="team", owner=Scope(user="alice")))
    resolver = _resolver(manager, ttl_seconds=0.0)

    facts = resolver.facts("acme", "team")
    assert facts.is_individual is True  # 无成员记录即个体空间
    assert facts.info is not None
    assert facts.info.owners == [Scope(org="acme", space="team", user="alice")]

    past = datetime.now(timezone.utc) - timedelta(days=1)
    manager.add_member(
        "acme",
        "team",
        SpaceMember(scope=Scope(user="bob"), expires_at=past),
    )
    facts = resolver.facts("acme", "team")
    # 归属主体在首条成员写入时被补写，过期的 bob 不参与判定
    assert [m.scope.user for m in facts.members] == ["alice"]
    assert facts.members[0].governance_role == SpaceGovernanceRole.OWNER
    assert facts.is_individual is False


def test_facts_returns_none_info_for_missing_space() -> None:
    resolver = _resolver(_manager(), ttl_seconds=0.0)
    facts = resolver.facts("acme", "absent")
    assert facts.info is None
    assert facts.members == ()


def test_cache_hit_and_invalidate() -> None:
    manager = _manager()
    manager.create(SpaceSpec(org="acme", space="team"))
    resolver = _resolver(manager, ttl_seconds=60.0)

    assert resolver.facts("acme", "team").is_individual is True
    manager.add_member("acme", "team", SpaceMember(scope=Scope(user="bob")))
    # TTL 未到：仍是缓存结果，授权变更的最大延迟由 TTL 决定
    assert resolver.facts("acme", "team").is_individual is True

    resolver.invalidate("acme", "team")
    facts = resolver.facts("acme", "team")
    assert [m.scope.user for m in facts.members] == ["bob"]
    assert facts.members[0].content_role == SpaceContentRole.CONTRIBUTOR

    resolver.invalidate("acme")  # 整个 org 失效
    assert resolver.facts("acme", "team").members[0].scope.user == "bob"


def test_cache_evicts_beyond_max_entries() -> None:
    manager = _manager()
    for name in ("s1", "s2", "s3"):
        manager.create(SpaceSpec(org="acme", space=name))
    resolver = _resolver(manager, ttl_seconds=60.0, max_entries=2)
    for name in ("s1", "s2", "s3"):
        resolver.facts("acme", name)
    assert resolver.cached_space_count == 2  # 断言 LRU 上限生效


def test_backend_error_propagates_instead_of_serving_stale_facts() -> None:
    """后端异常直接向上抛，由鉴权点按拒绝处理——沿用过期结果即为放行方向的失效。"""

    class _BrokenSpace(KVSpaceManager):
        def get(self, org: str, space: str):
            raise BackendError("kv", "unavailable")

    kv = InMemoryKVStore()
    manager = _BrokenSpace(CompositeStorage(kv=kv))
    resolver = _resolver(manager, ttl_seconds=60.0)
    with pytest.raises(BackendError):
        resolver.facts("acme", "team")


def test_spaces_for_delegates_to_the_reverse_index() -> None:
    manager = _manager()
    manager.create(SpaceSpec(org="acme", space="u-alice", owner=Scope(user="alice")))
    manager.create(SpaceSpec(org="acme", space="team"))
    manager.add_member("acme", "team", SpaceMember(scope=Scope(user="alice")))
    resolver = _resolver(manager)

    assert resolver.spaces_for(Scope(org="acme", user="alice"), "acme") == ("team", "u-alice")
    assert resolver.spaces_for(Scope(org="acme", user="bob"), "acme") == ()
