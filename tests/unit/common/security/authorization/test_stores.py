"""GrantStore 与 DelegationStore 两套实现的契约测试。

内存与 SQLite 两个后端跑**同一批用例**：它们背后是同一份契约，分开写两份测试的结果
是其中一份先漂——通常是 SQLite 那份，因为它改起来更麻烦。

测的是契约行为（软撤销、存储层滤时效、空 id 不查表、按 id 幂等），不测实现细节
（表结构、锁、序列化格式）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.security.authorization.authorization_impl.memory_stores import (
    InMemoryDelegationStore,
    InMemoryGrantStore,
)
from jiuwen_memory.common.security.authorization.authorization_impl.sqlite_stores import (
    SQLiteDelegationStore,
    SQLiteGrantStore,
)
from jiuwen_memory.common.security.authorization.store import (
    DelegationStore,
    DelegationStoreProducer,
    GrantStore,
    GrantStoreProducer,
)
from jiuwen_memory.common.security.types import Action, Delegation, Grant
from jiuwen_memory.common.type_def import Scope

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
ALICE = Scope(org="acme", space="main", user="alice")
BOB = Scope(org="acme", space="main", user="bob")

_GRANT_BACKENDS = [InMemoryGrantStore, SQLiteGrantStore]
_DELEGATION_BACKENDS = [InMemoryDelegationStore, SQLiteDelegationStore]


@pytest.fixture(params=_GRANT_BACKENDS, ids=["memory", "sqlite"])
def grant_store(request) -> GrantStore:
    if request.param is SQLiteGrantStore:
        return SQLiteGrantStore(":memory:")
    return InMemoryGrantStore()


@pytest.fixture(params=_DELEGATION_BACKENDS, ids=["memory", "sqlite"])
def delegation_store(request) -> DelegationStore:
    if request.param is SQLiteDelegationStore:
        return SQLiteDelegationStore(":memory:")
    return InMemoryDelegationStore()


def _grant(
    grant_id: str = "g1",
    *,
    grantor: Scope = ALICE,
    grantee: Scope = BOB,
    actions: frozenset[Action] = frozenset({Action.READ}),
    expires_at: datetime | None = None,
) -> Grant:
    return Grant(
        grant_id=grant_id,
        grantor=grantor,
        grantee=grantee,
        actions=actions,
        expires_at=expires_at,
    )


def _delegation(
    delegation_id: str = "d1",
    *,
    delegator: Scope = ALICE,
    delegate: Scope = Scope(org="acme", space="main", user="alice", agent="assistant"),
    actions: frozenset[Action] = frozenset({Action.READ, Action.WRITE}),
    expires_at: datetime = NOW + timedelta(hours=1),
    **kwargs,
) -> Delegation:
    return Delegation(
        delegation_id=delegation_id,
        delegator=delegator,
        delegate=delegate,
        actions=actions,
        expires_at=expires_at,
        **kwargs,
    )


def _find(store: GrantStore, *, action: Action = Action.READ, now: datetime = NOW) -> list[Grant]:
    return store.find_active(grantee=BOB, grantor_org="acme", action=action, now=now)


# ====================================================================== #
# GrantStore
# ====================================================================== #


def test_grant_round_trips(grant_store: GrantStore) -> None:
    """写进去的字段要原样读回来——scope 五维、动作集合、有效期一个都不能丢。"""
    grant = _grant(
        grantor=Scope(org="acme", space="main", user="alice", agent="a1", session="s1"),
        actions=frozenset({Action.READ, Action.WRITE}),
        expires_at=NOW + timedelta(days=1),
    )
    grant_store.add(grant)
    found = _find(grant_store)
    assert len(found) == 1
    assert found[0].grant_id == "g1"
    assert found[0].grantor == grant.grantor
    assert found[0].grantee == BOB
    assert found[0].actions == frozenset({Action.READ, Action.WRITE})
    assert found[0].expires_at == grant.expires_at


def test_grant_add_is_idempotent_by_id(grant_store: GrantStore) -> None:
    """同 id 写两次是一条而不是两条：重试写入不该在库里留下副本。"""
    grant_store.add(_grant())
    grant_store.add(_grant(actions=frozenset({Action.READ, Action.WRITE})))
    found = _find(grant_store)
    assert len(found) == 1
    assert found[0].actions == frozenset({Action.READ, Action.WRITE})


def test_grant_update_can_revoke_but_not_resurrect(grant_store):
    grant = _grant()
    grant_store.add(grant)
    grant_store.add(replace(grant, revoked=True))
    assert _find(grant_store) == []
    grant_store.add(grant)
    assert _find(grant_store) == []


def test_delegation_updates_security_bindings(delegation_store):
    record = _delegation(bound_credential_id="old")
    delegation_store.add(record)
    changed = replace(record, bound_credential_id="new", bound_session="new-session")
    delegation_store.add(changed)
    assert delegation_store.get(record.delegation_id) == changed
    delegation_store.add(replace(changed, revoked=True))
    assert delegation_store.get(record.delegation_id).revoked
    delegation_store.add(record)
    assert delegation_store.get(record.delegation_id).revoked


@pytest.mark.parametrize(
    "spaces",
    [
        frozenset(),
        frozenset({","}),
        frozenset({"space,other"}),
        frozenset({"a", "b"}),
        frozenset({'["a"]', "中文"}),
    ],
)
def test_delegation_allowed_spaces_round_trip_is_lossless(delegation_store, spaces):
    record = _delegation(allowed_spaces=spaces)
    delegation_store.add(record)
    assert delegation_store.get(record.delegation_id).allowed_spaces == spaces


def test_same_id_replaces_all_active_fields(grant_store, delegation_store):
    # 私有撤权快照用于核实同 ID 更新的全部字段。
    # pylint: disable=protected-access
    original = _grant()
    grant_store.add(original)
    updated = replace(
        original,
        grantor=Scope(org="other", user="new"),
        grantee=Scope(org="other", agent="bot"),
        actions=frozenset({Action.WRITE}),
        expires_at=NOW + timedelta(hours=2),
    )
    grant_store.add(updated)
    assert grant_store._get_for_revoke(updated.grant_id) == updated
    delegation = _delegation()
    delegation_store.add(delegation)
    changed = replace(
        delegation,
        delegator=updated.grantor,
        delegate=updated.grantee,
        actions=updated.actions,
        expires_at=updated.expires_at,
        not_before=NOW,
        allowed_spaces=frozenset({"x,y"}),
        bound_credential_id="new",
        bound_session="s2",
    )
    delegation_store.add(changed)
    assert delegation_store.get(changed.delegation_id) == changed


def test_conditional_revoke_cannot_revoke_a_retargeted_id(grant_store):
    # 白盒验证检查/执行之间换归属的原子性。
    # pylint: disable=protected-access
    original = _grant()
    grant_store.add(original)
    snapshot = grant_store._get_for_revoke(original.grant_id)
    changed = replace(original, grantor=Scope(org="elsewhere", user="victim"))
    grant_store.add(changed)
    with pytest.raises(BackendError, match="changed"):
        grant_store._revoke_bound(snapshot.grant_id, snapshot.grantor)
    assert not grant_store._get_for_revoke(original.grant_id).revoked


def test_mutable_scope_alias_cannot_change_stored_grant_target(grant_store):
    # 白盒验证私有快照也不能返回可变内部别名。
    # pylint: disable=protected-access
    original = _grant(grantor=Scope(org="acme", user="owner"))
    grant_store.add(original)
    original.grantor.user = "mutated-input"
    snapshot = grant_store._get_for_revoke(original.grant_id)
    assert snapshot.grantor.user == "owner"
    snapshot.grantor.user = "mutated-output"
    assert grant_store._get_for_revoke(original.grant_id).grantor.user == "owner"


@pytest.mark.parametrize(
    "raw,expected",
    [("", frozenset()), ("main", frozenset({"main"})), (",", None), ("main,other", None)],
)
def test_sqlite_legacy_spaces_migrate_without_guessing(tmp_path, raw, expected):
    path = str(tmp_path / "legacy.db")
    store = SQLiteDelegationStore(path)
    store.add(_delegation())
    store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE auth_delegations SET allowed_spaces=?", (raw,))
        conn.execute("ALTER TABLE auth_delegations DROP COLUMN allowed_spaces_encoding")
    store = SQLiteDelegationStore(path)
    try:
        if expected is None:
            with pytest.raises(BackendError, match="legacy"):
                store.get("d1")
        else:
            assert store.get("d1").allowed_spaces == expected
        store.add(_delegation(allowed_spaces=frozenset({"space,other"})))
        assert store.get("d1").allowed_spaces == frozenset({"space,other"})
    finally:
        store.close()


@pytest.mark.parametrize("raw", ["broken", "null", "{}", "[42]"])
def test_sqlite_corrupt_json_spaces_fail_closed(raw):
    # 故障注入必须绕过正常 Store 写入校验。
    # pylint: disable=protected-access
    store = SQLiteDelegationStore(":memory:")
    try:
        store.add(_delegation())
        store._conn.execute("UPDATE auth_delegations SET allowed_spaces=?", (raw,))
        with pytest.raises(BackendError):
            store.get("d1")
    finally:
        store.close()


def test_revoked_grant_is_not_returned(grant_store: GrantStore) -> None:
    grant_store.add(_grant())
    grant_store.revoke("g1")
    assert _find(grant_store) == []


def test_revoke_is_idempotent(grant_store: GrantStore) -> None:
    grant_store.add(_grant())
    grant_store.revoke("g1")
    grant_store.revoke("g1")
    assert _find(grant_store) == []


def test_revoke_unknown_grant_is_silent(grant_store: GrantStore) -> None:
    """撤销不存在的 id 不抛：撤销是幂等操作，重放不该变成错误。"""
    grant_store.revoke("never-existed")


def test_expired_grant_is_filtered_by_the_store(grant_store: GrantStore) -> None:
    """时效在**存储层**就滤掉（契约要求），不是留给 Authorizer 筛。"""
    grant_store.add(_grant(expires_at=NOW - timedelta(seconds=1)))
    assert _find(grant_store) == []


def test_grant_expiring_exactly_now_is_inactive(grant_store: GrantStore) -> None:
    """``expires_at == now`` 判失效——边界向「更严」的一侧靠。"""
    grant_store.add(_grant(expires_at=NOW))
    assert _find(grant_store) == []


def test_grant_without_expiry_stays_active(grant_store: GrantStore) -> None:
    """Grant 允许长期有效（与 Delegation 的区别之一）。"""
    grant_store.add(_grant(expires_at=None))
    found = _find(grant_store, now=NOW + timedelta(days=3650))
    assert len(found) == 1
    assert found[0].expires_at is None


def test_grant_for_another_action_is_not_returned(grant_store: GrantStore) -> None:
    grant_store.add(_grant(actions=frozenset({Action.READ})))
    assert _find(grant_store, action=Action.DELETE) == []


def test_action_match_is_not_substring_match(grant_store: GrantStore) -> None:
    """``read_audit`` 不能被 ``read`` 的查询命中。

    动作集合在 SQLite 里存成逗号串，用 ``LIKE '%read%'`` 筛就会把 ``read_audit``
    一起捞出来——一条只开放了读审计的授权会变成能读数据。
    """
    grant_store.add(_grant(actions=frozenset({Action.READ_AUDIT})))
    assert _find(grant_store, action=Action.READ) == []
    assert len(_find(grant_store, action=Action.READ_AUDIT)) == 1


def test_grant_from_another_org_is_not_returned(grant_store: GrantStore) -> None:
    """org 是硬边界，存储查询就按它收窄。"""
    grant_store.add(_grant(grantor=Scope(org="globex", space="main", user="carol")))
    assert _find(grant_store) == []


def test_grant_to_another_org_grantee_is_not_returned(grant_store: GrantStore) -> None:
    grant_store.add(_grant(grantee=Scope(org="globex", space="main", user="bob")))
    assert _find(grant_store) == []


def test_multiple_grants_are_all_returned(grant_store: GrantStore) -> None:
    """同一对主体可以有多条授权，查询不做去重——挑哪条由 Authorizer 按覆盖规则定。"""
    grant_store.add(_grant("g1"))
    grant_store.add(_grant("g2", grantor=Scope(org="acme", space="main", user="dave")))
    assert {g.grant_id for g in _find(grant_store)} == {"g1", "g2"}


def test_grant_store_health_passes(grant_store: GrantStore) -> None:
    assert grant_store.health() is None


def test_revoked_grant_cannot_be_resurrected_by_replay(grant_store: GrantStore) -> None:
    """撤销后用同 id 重放旧创建请求不得复活授权（P1-4）。

    两个后端必须同语义：memory 不复活，sqlite 的 upsert 也不动 revoked_at。
    """
    grant_store.add(_grant())
    grant_store.revoke("g1")
    grant_store.add(_grant())  # 模拟重放旧 create
    assert _find(grant_store) == []


# ====================================================================== #
# DelegationStore
# ====================================================================== #


def test_delegation_round_trips(delegation_store: DelegationStore) -> None:
    delegation = _delegation(
        not_before=NOW - timedelta(minutes=5),
        allowed_spaces=frozenset({"main", "scratch"}),
        bound_credential_id="cred-7",
        bound_session="sess-9",
    )
    delegation_store.add(delegation)
    loaded = delegation_store.get("d1")
    assert loaded == delegation


def test_delegation_add_is_idempotent_by_id(delegation_store: DelegationStore) -> None:
    delegation_store.add(_delegation())
    delegation_store.add(_delegation(actions=frozenset({Action.READ})))
    loaded = delegation_store.get("d1")
    assert loaded is not None
    assert loaded.actions == frozenset({Action.READ})


def test_missing_delegation_returns_none(delegation_store: DelegationStore) -> None:
    assert delegation_store.get("nope") is None


def test_empty_delegation_id_returns_none(delegation_store: DelegationStore) -> None:
    """``AuthContext.delegation_id`` 默认是空串。

    让空 id 去查表，就意味着一条 id 为空的记录能被任何**没有**声明委托的请求命中。
    """
    delegation_store.add(_delegation(""))
    assert delegation_store.get("") is None


def test_revoked_delegation_is_returned_with_the_flag_set(
    delegation_store: DelegationStore,
) -> None:
    """撤销后记录**仍然读得到**，只是 ``revoked=True``。

    与 GrantStore 的差别是有意的：``get`` 返回原始记录，有效性由 Authorizer 用本次
    判定的同一个 ``now`` 来判（见 :meth:`DelegationStore.get` 契约）。存储自己判会和
    Grant 的时效判定错开。
    """
    delegation_store.add(_delegation())
    delegation_store.revoke("d1")
    loaded = delegation_store.get("d1")
    assert loaded is not None
    assert loaded.revoked is True
    assert not loaded.is_active(now=NOW)


def test_revoke_preserves_the_rest_of_the_record(delegation_store: DelegationStore) -> None:
    """撤销只改一个标记，其余字段原样保留——审计要能回答「这条委托原本能做什么」。"""
    delegation_store.add(
        _delegation(
            allowed_spaces=frozenset({"main"}),
            bound_credential_id="cred-7",
            bound_session="sess-9",
        )
    )
    delegation_store.revoke("d1")
    loaded = delegation_store.get("d1")
    assert loaded is not None
    assert loaded.delegator == ALICE
    assert loaded.actions == frozenset({Action.READ, Action.WRITE})
    assert loaded.allowed_spaces == frozenset({"main"})
    assert loaded.bound_credential_id == "cred-7"
    assert loaded.bound_session == "sess-9"


def test_delegation_revoke_is_idempotent(delegation_store: DelegationStore) -> None:
    delegation_store.add(_delegation())
    delegation_store.revoke("d1")
    delegation_store.revoke("d1")
    loaded = delegation_store.get("d1")
    assert loaded is not None
    assert loaded.revoked is True


def test_revoke_unknown_delegation_is_silent(delegation_store: DelegationStore) -> None:
    delegation_store.revoke("never-existed")


def test_expired_delegation_round_trips_as_inactive(delegation_store: DelegationStore) -> None:
    """过期委托同样读得回来——由 Authorizer 判失效，理由同撤销。"""
    delegation_store.add(_delegation(expires_at=NOW - timedelta(seconds=1)))
    loaded = delegation_store.get("d1")
    assert loaded is not None
    assert not loaded.is_active(now=NOW)


def test_delegation_store_health_passes(delegation_store: DelegationStore) -> None:
    assert delegation_store.health() is None


def test_revoked_delegation_cannot_be_resurrected_by_replay(
    delegation_store: DelegationStore,
) -> None:
    """撤销后用同 id 重放旧创建请求不得恢复代操作关系（P1-4，同 Grant 语义）。"""
    delegation_store.add(_delegation())
    delegation_store.revoke("d1")
    delegation_store.add(_delegation())  # 模拟重放旧 create
    loaded = delegation_store.get("d1")
    assert loaded is not None
    assert loaded.revoked is True


# ====================================================================== #
# 装配
# ====================================================================== #


@pytest.mark.parametrize("target", ["memory", "sqlite"])
def test_stores_are_registered(target: str) -> None:
    """两个后端都能从注册名装出来（F05 §独立 Producer）。"""
    from jiuwen_memory.common.security.bootstrap import register_security

    register_security()
    assert target in GrantStoreProducer.known()
    assert target in DelegationStoreProducer.known()


def test_sqlite_store_persists_across_instances(tmp_path) -> None:
    """SQLite 后端跨实例可见——内存后端做不到，这是选它的唯一理由。"""
    db = str(tmp_path / "auth.db")
    writer = SQLiteGrantStore(db)
    writer.add(_grant())
    writer.close()

    reader = SQLiteGrantStore(db)
    try:
        assert len(_find(reader)) == 1
    finally:
        reader.close()


def test_sqlite_store_ignores_unknown_actions(tmp_path) -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    """库里存着核心不认识的动作名时，跳过该动作而不是让整条查询失败。

    降级部署（新版写入、旧版读取）会造出这种记录。认不出就当没有，是 F05 §授权
    不变量 5「新 Action 默认拒绝」在存储层的形态；抛异常则会让一条脏记录瘫掉所有
    授权查询。
    """
    db = str(tmp_path / "auth.db")
    store = SQLiteGrantStore(db)
    try:
        store.add(_grant(actions=frozenset({Action.READ})))
        store._conn.execute(  # noqa: SLF001 — 造脏数据只能绕过写入路径
            "UPDATE auth_grants SET actions=? WHERE grant_id=?", ("read,teleport", "g1")
        )
        found = _find(store)
        assert len(found) == 1
        assert found[0].actions == frozenset({Action.READ})
    finally:
        store.close()


def test_sqlite_store_wraps_backend_failures(tmp_path) -> None:
    """sqlite3 故障升格为 BackendError：503 语义，不落到通用 500（审核 P2-1）。

    关库后一切操作都因 ``sqlite3.ProgrammingError`` 失败——那是「依赖不可用」，不是
    调用方错误也不是代码缺陷。裸传播会让 dispatch 把它当 500（内部错误）分流。
    """
    store = SQLiteGrantStore(str(tmp_path / "auth.db"))
    store.close()

    with pytest.raises(BackendError):
        store.add(_grant())
    with pytest.raises(BackendError):
        store.health()
