from __future__ import annotations

import json
from dataclasses import replace

import pytest

from deploy.migration.backfill import (
    Backfiller,
    audit_shape,
    provision_main_spaces,
    rebuild_index,
    rebuild_registry,
)
from jiuwen_memory.common.security.principal import AUTHOR_AGENT, AUTHOR_PRINCIPAL
from jiuwen_memory.common.type_def import MemoryUnit, Scope, memory_key
from jiuwen_memory.common.type_def.memory import Segment
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.control.space_impl.kv_space_manager import (
    _MEMBER_PREFIX,
    _MEMBERS_KEY,
    KVSpaceManager,
    _scope,
)
from jiuwen_memory.control.types import SpaceMember, SpaceSpec
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage

pytestmark = pytest.mark.unit


def _legacy_unit(unit_id: str, scope: Scope) -> MemoryUnit:
    return MemoryUnit(id=unit_id, scope=scope, segments=[Segment(content=f"c-{unit_id}")])


def _seed(kv: InMemoryKVStore) -> KVSpaceManager:
    """造一份升级前形态的数据：条目 scope 带主体维、空间无归属登记、成员逐键。"""
    manager = KVSpaceManager(CompositeStorage(kv=kv))
    manager.create(SpaceSpec(org="acme", space="team"))
    alice = Scope(org="acme", space="team", user="alice", session="s1")
    kv.insert(alice, memory_key("u1"), dumps(_legacy_unit("u1", alice)))
    kv.insert(alice, "/messages/m1", b"raw-message")
    kv.insert(
        _scope("acme", "team"),
        f"{_MEMBER_PREFIX}legacy",
        json.dumps(
            {"scope": {"org": "acme", "space": "team", "user": "bob"}, "role": "viewer"},
            ensure_ascii=False,
        ).encode("utf-8"),
    )
    return manager


def test_backfill_performs_all_four_actions() -> None:
    kv = InMemoryKVStore()
    manager = _seed(kv)

    report = Backfiller(kv).backfill_space("acme", "team")
    target = _scope("acme", "team")

    # 项 1：归属登记按条目 scope 的主体维推导
    assert manager.get("acme", "team").owners == [
        Scope(org="acme", space="team", user="alice")
    ]
    # 项 3：条目落在两维 scope 下，旧物理键已删
    migrated = loads(kv.get(target, memory_key("u1")))
    assert migrated is not None and migrated.scope == target
    assert kv.scan(Scope(org="acme", space="team", user="alice", session="s1")) == []
    assert kv.get(target, "/messages/m1") == b"raw-message"
    # 项 2：作者标记按主体维推导
    assert migrated.system_metadata[AUTHOR_PRINCIPAL] == "user:alice"
    assert migrated.system_metadata[AUTHOR_AGENT] == ""
    # 项 4：逐成员键并成单键
    assert kv.scan(target, prefix=_MEMBER_PREFIX) == []
    assert [m.scope.user for m in manager.list_members("acme", "team")] == ["bob"]
    # 反查索引由归属登记与成员记录重建
    assert manager.index.spaces_for(Scope(org="acme", user="alice"), "acme") == ("team",)
    assert manager.index.spaces_for(Scope(org="acme", user="bob"), "acme") == ("team",)

    assert report.units_migrated == 1
    assert report.units_marked == 1
    assert report.members_merged == 1


def test_backfill_is_idempotent_on_rerun() -> None:
    kv = InMemoryKVStore()
    _seed(kv)
    backfiller = Backfiller(kv)
    backfiller.backfill_space("acme", "team")

    second = backfiller.backfill_space("acme", "team")
    assert second.units_migrated == 0  # 无旧键可搬
    assert second.units_marked == 0  # 标记已存在，不改写
    assert second.members_merged == 0
    assert second.owners_registered == [Scope(org="acme", space="team", user="alice")]


def test_dry_run_reports_without_writing() -> None:
    kv = InMemoryKVStore()
    manager = _seed(kv)
    before = sorted(kv.scopes(), key=str)

    report = Backfiller(kv, dry_run=True).backfill_space("acme", "team")

    assert report.units_migrated == 1
    assert report.owners_registered == [Scope(org="acme", space="team", user="alice")]
    assert manager.get("acme", "team").owners == []  # 未落盘
    assert sorted(kv.scopes(), key=str) == before
    assert kv.scan(_scope("acme", "team"), prefix=_MEMBER_PREFIX)  # 旧成员键仍在


def test_multi_owner_space_is_reported_for_manual_handling() -> None:
    kv = InMemoryKVStore()
    manager = _seed(kv)
    bob = Scope(org="acme", space="team", user="bob")
    kv.insert(bob, memory_key("u2"), dumps(_legacy_unit("u2", bob)))

    report = Backfiller(kv).backfill_space("acme", "team")
    assert len(manager.get("acme", "team").owners) == 2
    assert any("多归属空间" in item for item in report.unresolved)


def test_rebuild_registry_and_index_run_standalone() -> None:
    kv = InMemoryKVStore()
    manager = _seed(kv)
    # 抹掉注册表模拟存量空间
    for key, _ in list(kv.scan(Scope(), prefix="/spaces/by-id/")):
        kv.delete(Scope(), key)

    assert rebuild_registry(kv, "acme") == ["team"]
    assert rebuild_registry(kv, "acme") == []  # 幂等

    manager.add_member("acme", "team", SpaceMember(scope=Scope(user="carol")))
    assert rebuild_index(kv, "acme")["team"] >= 1


def test_entries_without_principal_dims_are_reported_instead_of_guessed() -> None:
    """条目已在两维 scope 下却没有作者标记：作者无从推导，须人工处置而不是猜。"""
    kv = InMemoryKVStore()
    manager = KVSpaceManager(CompositeStorage(kv=kv))
    manager.create(SpaceSpec(org="acme", space="team"))
    target = _scope("acme", "team")
    kv.insert(target, memory_key("u9"), dumps(_legacy_unit("u9", target)))

    report = Backfiller(kv).backfill_space("acme", "team")
    assert any("缺作者标记" in item for item in report.unresolved)
    assert report.units_marked == 0


def test_provision_main_spaces_registers_each_principal_as_owner() -> None:
    from jiuwen_memory.api.memory_api_impl import build_kernel

    kernel = build_kernel()
    assert kernel.space is not None
    created = provision_main_spaces(
        kernel, "acme", ["user:alice", "# 注释", "", "agent:a1", "bad-line"]
    )

    assert created == ["u-alice", "a-a1"]
    assert kernel.space.get("acme", "u-alice").owners == [
        Scope(org="acme", space="u-alice", user="alice")
    ]
    assert kernel.space.get("acme", "a-a1").owners == [
        Scope(org="acme", space="a-a1", agent="a1")
    ]


def test_audit_shape_lists_shapes_needing_manual_handling() -> None:
    kv = InMemoryKVStore()
    _seed(kv)
    kv.insert(
        _scope("acme", "team"),
        _MEMBERS_KEY,
        json.dumps(
            [
                {
                    "scope": {"org": "acme", "space": "team", "user": "d", "agent": "a1"},
                    "content_role": "viewer",
                },
                {
                    "scope": {"org": "acme", "space": "team", "user": "e", "session": "s"},
                    "content_role": "viewer",
                },
            ],
            ensure_ascii=False,
        ).encode("utf-8"),
    )

    findings = audit_shape(kv, "acme")
    assert findings["member_with_both_principal_dims"]
    assert findings["member_with_session"]
    assert findings["unmigrated_entry_scopes"]  # 条目 scope 尚未迁移
    assert findings["multi_owner_spaces"] == []


def test_route_tag_keys_are_filled_with_empty_strings_for_legacy_entries() -> None:
    """项 5：存量条目补齐判定标签键，取值一律空串。

    不补的后果是升级那一刻起全部历史记忆在带 coords 的检索里凭空消失：集合谓词
    ``IN ("", value)`` 在字段缺失时判为不匹配，条目查不到且不报错。

    只能补空串——存量条目写入时没有归属坐标，「这条属于哪个项目」不存在于任何地方。
    空串的语义是「不特定于任何坐标，因此对任何坐标都可见」。
    """
    kv = InMemoryKVStore()
    _seed(kv)

    Backfiller(kv, tag_keys=frozenset({"project_id", "session_id"})).backfill_space(
        "acme", "team"
    )

    migrated = loads(kv.get(_scope("acme", "team"), memory_key("u1")))
    assert migrated is not None
    assert migrated.system_metadata["project_id"] == ""
    assert migrated.system_metadata["session_id"] == ""


def test_filling_tag_keys_never_overwrites_an_existing_value() -> None:
    """已有取值不覆盖：该键可能已由新写入路径写过，覆盖即改变条目的可见性。"""
    kv = InMemoryKVStore()
    _seed(kv)
    tag_keys = frozenset({"project_id"})
    Backfiller(kv, tag_keys=tag_keys).backfill_space("acme", "team")

    target = _scope("acme", "team")
    unit = loads(kv.get(target, memory_key("u1")))
    kv.update(
        target,
        memory_key("u1"),
        dumps(replace(unit, system_metadata={**unit.system_metadata, "project_id": "p1"})),
    )

    second = Backfiller(kv, tag_keys=tag_keys).backfill_space("acme", "team")
    kept = loads(kv.get(target, memory_key("u1")))
    assert kept is not None and kept.system_metadata["project_id"] == "p1"
    assert second.units_marked == 0


def test_an_empty_tag_key_set_skips_the_fifth_action() -> None:
    """未装配 router 时判定标签键集合为空，第 5 项整体跳过。"""
    kv = InMemoryKVStore()
    _seed(kv)

    Backfiller(kv).backfill_space("acme", "team")

    migrated = loads(kv.get(_scope("acme", "team"), memory_key("u1")))
    assert migrated is not None
    assert set(migrated.system_metadata) == {AUTHOR_PRINCIPAL, AUTHOR_AGENT}
