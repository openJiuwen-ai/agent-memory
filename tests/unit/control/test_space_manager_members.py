from __future__ import annotations

import json

import pytest

from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.security.space_roles import SpaceContentRole, SpaceGovernanceRole
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.space_impl.kv_space_manager import (
    _MEMBER_PREFIX,
    _MEMBERS_KEY,
    _MEMBERS_LIMIT,
    KVSpaceManager,
    _scope,
)
from jiuwen_memory.control.types import SpaceMember, SpacePatch, SpaceSpec, SpaceStatus
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage

pytestmark = pytest.mark.unit


def _manager() -> tuple[KVSpaceManager, InMemoryKVStore]:
    kv = InMemoryKVStore()
    return KVSpaceManager(CompositeStorage(kv=kv)), kv


def test_owners_survive_the_serialization_round_trip() -> None:
    """归属登记的编解码往返。

    ``SpaceInfo`` 的编解码是手写的逐字段映射，漏列即 owners 在序列化处被丢弃、读回恒为空，
    归属对比整体不生效，且症状与「回填未完成」无法区分。
    """
    manager, _ = _manager()
    created = manager.create(SpaceSpec(org="acme", space="u-alice", owner=Scope(user="alice")))
    assert created.owners == [Scope(org="acme", space="u-alice", user="alice")]
    assert manager.get("acme", "u-alice").owners == created.owners

    # 归属主体取单维：调用方带两维时只登记 user
    manager.create(
        SpaceSpec(org="acme", space="mixed", owner=Scope(user="alice", agent="a1")),
    )
    assert manager.get("acme", "mixed").owners == [
        Scope(org="acme", space="mixed", user="alice")
    ]

    # 不传归属主体即不登记（共享空间形态）
    assert manager.create(SpaceSpec(org="acme", space="team")).owners == []


def test_create_registers_the_reverse_index_entry() -> None:
    manager, _ = _manager()
    manager.create(SpaceSpec(org="acme", space="u-alice", owner=Scope(user="alice")))
    assert manager.spaces_for(Scope(org="acme", user="alice"), "acme") == ("u-alice",)


def test_members_live_in_a_single_key_with_both_axes() -> None:
    manager, kv = _manager()
    manager.create(SpaceSpec(org="acme", space="team"))
    manager.add_member(
        "acme",
        "team",
        SpaceMember(
            scope=Scope(user="alice"),
            content_role=SpaceContentRole.EDITOR,
            governance_role=SpaceGovernanceRole.MANAGER,
        ),
    )
    manager.add_member("acme", "team", SpaceMember(scope=Scope(agent="a1")))

    payload = json.loads(kv.get(_scope("acme", "team"), _MEMBERS_KEY).decode("utf-8"))
    assert len(payload) == 2  # 整表落单键
    assert {item["content_role"] for item in payload} == {"editor", "contributor"}
    assert not kv.scan(_scope("acme", "team"), prefix=_MEMBER_PREFIX)  # 无逐成员键

    members = {m.scope.user or m.scope.agent: m for m in manager.list_members("acme", "team")}
    assert members["alice"].governance_role == SpaceGovernanceRole.MANAGER
    assert members["a1"].content_role == SpaceContentRole.CONTRIBUTOR


def test_legacy_single_axis_roles_are_mapped_on_read() -> None:
    manager, kv = _manager()
    manager.create(SpaceSpec(org="acme", space="team"))
    legacy = []
    for user, role in (
        ("o", "owner"),
        ("a", "admin"),
        ("m", "member"),
        ("v", "viewer"),
        ("x", "unknown-role"),
    ):
        legacy.append({"scope": {"org": "acme", "space": "team", "user": user}, "role": role})
    kv.insert(
        _scope("acme", "team"),
        _MEMBERS_KEY,
        json.dumps(legacy, ensure_ascii=False).encode("utf-8"),
    )

    parsed = {m.scope.user: m for m in manager.list_members("acme", "team")}
    assert (parsed["o"].content_role, parsed["o"].governance_role) == (
        SpaceContentRole.EDITOR,
        SpaceGovernanceRole.OWNER,
    )
    assert (parsed["a"].content_role, parsed["a"].governance_role) == (
        SpaceContentRole.EDITOR,
        SpaceGovernanceRole.MANAGER,
    )
    assert parsed["m"].content_role == SpaceContentRole.CONTRIBUTOR
    assert parsed["v"].content_role == SpaceContentRole.VIEWER
    # 未识别取值按最低档处置而非抛异常：拒绝解析会使存量空间整体不可用
    assert (parsed["x"].content_role, parsed["x"].governance_role) == (
        SpaceContentRole.VIEWER,
        SpaceGovernanceRole.NONE,
    )


def test_member_scope_shape_is_enforced() -> None:
    manager, _ = _manager()
    manager.create(SpaceSpec(org="acme", space="team"))
    with pytest.raises(ValidationError):  # 主体两维同时非空
        manager.add_member(
            "acme", "team", SpaceMember(scope=Scope(user="alice", agent="a1"))
        )
    with pytest.raises(ValidationError):  # 会话维不进成员记录
        manager.add_member("acme", "team", SpaceMember(scope=Scope(user="alice", session="s1")))
    with pytest.raises(ValidationError):  # 跨 org
        manager.add_member("acme", "team", SpaceMember(scope=Scope(org="other", user="alice")))


def test_first_member_backfills_the_owner_entry() -> None:
    """空间由个体转共享的瞬间补写归属主体，否则它在成员表非空后失去归属对比这条通路。"""
    manager, _ = _manager()
    manager.create(SpaceSpec(org="acme", space="team", owner=Scope(user="alice")))
    manager.add_member("acme", "team", SpaceMember(scope=Scope(user="bob")))

    members = {m.scope.user: m for m in manager.list_members("acme", "team")}
    assert set(members) == {"alice", "bob"}
    assert members["alice"].governance_role == SpaceGovernanceRole.OWNER
    assert members["alice"].content_role == SpaceContentRole.EDITOR


def test_owner_entry_merges_with_a_member_record_of_the_same_scope() -> None:
    """同 scope 合并为一条：写两条时后者覆盖前者，会静默丢掉归属主体的治理权。"""
    manager, _ = _manager()
    manager.create(SpaceSpec(org="acme", space="u-alice", owner=Scope(user="alice")))
    manager.add_member(
        "acme",
        "u-alice",
        SpaceMember(scope=Scope(user="alice"), content_role=SpaceContentRole.VIEWER),
    )

    members = manager.list_members("acme", "u-alice")
    assert len(members) == 1
    assert members[0].governance_role == SpaceGovernanceRole.OWNER  # 取较高档
    assert members[0].content_role == SpaceContentRole.VIEWER  # 内容轴按入参


def test_add_member_updates_an_existing_record_in_place() -> None:
    manager, _ = _manager()
    manager.create(SpaceSpec(org="acme", space="team"))
    manager.add_member("acme", "team", SpaceMember(scope=Scope(user="alice")))
    manager.add_member(
        "acme", "team", SpaceMember(scope=Scope(user="alice"), content_role=SpaceContentRole.EDITOR)
    )
    members = manager.list_members("acme", "team")
    assert len(members) == 1
    assert members[0].content_role == SpaceContentRole.EDITOR


def test_remove_member_clears_the_record_and_its_index_entry() -> None:
    manager, _ = _manager()
    manager.create(SpaceSpec(org="acme", space="team"))
    manager.add_member("acme", "team", SpaceMember(scope=Scope(user="alice")))
    assert manager.spaces_for(Scope(org="acme", user="alice"), "acme") == ("team",)

    manager.remove_member("acme", "team", Scope(user="alice"))
    assert manager.list_members("acme", "team") == []
    assert manager.spaces_for(Scope(org="acme", user="alice"), "acme") == ()


def test_remove_member_keeps_the_index_entry_of_a_registered_owner() -> None:
    """归属主体的索引项不随成员身份移除而删。

    用户级共享空间的正常形态就是归属主体同时持有成员记录（``add_member`` 的同 scope
    合并分支）。连带删掉索引项后，该主体凭归属登记仍持归属主体档，但按主体反查空间的
    候选集取自索引，她因此看不到自己的空间——遗漏方向的失效，索引契约不允许。
    """
    manager, _ = _manager()
    owner = Scope(org="acme", space="u-alice", user="alice")
    manager.create(SpaceSpec(org="acme", space="u-alice", owner=Scope(user="alice")))
    manager.add_member("acme", "u-alice", SpaceMember(scope=Scope(user="alice")))

    manager.remove_member("acme", "u-alice", Scope(user="alice"))
    assert manager.list_members("acme", "u-alice") == []
    assert manager.get("acme", "u-alice").owners == [owner]
    assert manager.spaces_for(Scope(org="acme", user="alice"), "acme") == ("u-alice",)


def test_add_member_rolls_back_the_member_table_when_the_index_write_fails() -> None:
    """索引写失败即回滚成员表：留成员记录而无索引项是遗漏方向的失效。"""
    manager, _ = _manager()
    manager.create(SpaceSpec(org="acme", space="team"))
    manager.add_member("acme", "team", SpaceMember(scope=Scope(user="alice")))

    class _IndexWriteFails(type(manager)):
        """索引写入已随反查折进 SpaceManager，注入失败点取覆写而非改实例属性。"""

        def _index_add(self, subject: Scope, space: str) -> None:
            raise BackendError("index unavailable")

    manager.__class__ = _IndexWriteFails
    with pytest.raises(BackendError):
        manager.add_member("acme", "team", SpaceMember(scope=Scope(user="bob")))

    assert [m.scope.user for m in manager.list_members("acme", "team")] == ["alice"]


def test_remove_member_keeps_the_index_entry_when_the_member_table_write_fails() -> None:
    """成员表写失败时索引项须留下：先删索引会造成遗漏，是超集契约唯一禁止的方向。

    次序反了的表现：成员记录仍在（该成员仍是成员），索引项已删，于是 ``spaces_for``
    反查不到这个空间，跨空间检索不显式给出候选空间时该空间整体不进候选集，
    且无报错、无审计差异。
    """
    manager, kv = _manager()
    manager.create(SpaceSpec(org="acme", space="team"))
    manager.add_member("acme", "team", SpaceMember(scope=Scope(user="alice")))

    def _boom(scope: Scope, key: str, value: bytes) -> None:
        raise BackendError("kv unavailable")

    kv.update = _boom  # type: ignore[method-assign]
    with pytest.raises(BackendError):
        manager.remove_member("acme", "team", Scope(user="alice"))

    assert manager.spaces_for(Scope(org="acme", user="alice"), "acme") == ("team",)


def test_delete_space_clears_reverse_index_entries() -> None:
    manager, _ = _manager()
    manager.create(SpaceSpec(org="acme", space="team", owner=Scope(user="alice")))
    manager.delete("acme", "team")
    assert manager.spaces_for(Scope(org="acme", user="alice"), "acme") == ()


def test_members_limit_is_enforced() -> None:
    manager, kv = _manager()
    manager.create(SpaceSpec(org="acme", space="team"))
    bulk = [
        {"scope": {"org": "acme", "space": "team", "user": f"u{i}"}, "content_role": "viewer"}
        for i in range(_MEMBERS_LIMIT)
    ]
    kv.insert(
        _scope("acme", "team"),
        _MEMBERS_KEY,
        json.dumps(bulk, ensure_ascii=False).encode("utf-8"),
    )
    with pytest.raises(ValidationError):
        manager.add_member("acme", "team", SpaceMember(scope=Scope(user="over-limit")))


def test_status_transitions_are_checked() -> None:
    manager, _ = _manager()
    manager.create(SpaceSpec(org="acme", space="team"))

    frozen = manager.update("acme", "team", SpacePatch(status=SpaceStatus.FROZEN))
    assert frozen.status == SpaceStatus.FROZEN
    # 冻结空间可由治理档成员解冻
    assert manager.update("acme", "team", SpacePatch(status=SpaceStatus.ACTIVE)).status == (
        SpaceStatus.ACTIVE
    )
    # DELETING / DELETED 只由删除流程内部置入
    with pytest.raises(ValidationError):
        manager.update("acme", "team", SpacePatch(status=SpaceStatus.DELETING))
    with pytest.raises(ValidationError):
        manager.update("acme", "team", SpacePatch(status=SpaceStatus.DELETED))
    # 同态是空操作
    assert manager.update("acme", "team", SpacePatch(status=SpaceStatus.ACTIVE)).status == (
        SpaceStatus.ACTIVE
    )


def test_create_rollback_leaves_no_registry_or_index_entry() -> None:
    class _FailingKV(InMemoryKVStore):
        def insert(self, scope, key, value, ttl: float = 0.0) -> None:
            if key == "/space/info":
                raise RuntimeError("boom")
            super().insert(scope, key, value, ttl)

    kv = _FailingKV()
    manager = KVSpaceManager(CompositeStorage(kv=kv))
    with pytest.raises(RuntimeError):
        manager.create(SpaceSpec(org="acme", space="team", owner=Scope(user="alice")))

    assert kv.scan(Scope()) == []  # 注册表与索引项均已撤销
