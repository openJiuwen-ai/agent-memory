"""GrantStore 与 DelegationStore 两套实现的契约测试。

内存与 SQLite 两个后端跑**同一批用例**：它们背后是同一份契约，分开写两份测试的结果
是其中一份先漂——通常是 SQLite 那份，因为它改起来更麻烦。

测的是契约行为（软撤销、存储层滤时效、空 id 不查表、按 id 幂等），不测实现细节
（表结构、锁、序列化格式）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from common.security.authorization.authorization_impl.memory_stores import (
    InMemoryDelegationStore,
    InMemoryGrantStore,
)
from common.security.authorization.authorization_impl.sqlite_stores import (
    SQLiteDelegationStore,
    SQLiteGrantStore,
)
from common.security.authorization.store import (
    DelegationStore,
    DelegationStoreProducer,
    GrantStore,
    GrantStoreProducer,
)
from common.security.types import Action, Delegation, Grant
from common.type_def import Scope

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
    from common.security.bootstrap import register_security

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
    """库里存着核心不认识的动作名时，跳过该动作而不是让整条查询失败。

    降级部署（新版写入、旧版读取）会造出这种记录。认不出就当没有，是 F05 §授权
    不变量 5「新 Action 默认拒绝」在存储层的形态；抛异常则会让一条脏记录瘫掉所有
    授权查询。
    """
    db = str(tmp_path / "auth.db")
    store = SQLiteGrantStore(db)
    try:
        store.add(_grant(actions=frozenset({Action.READ})))
        with sqlite3.connect(db) as connection:
            connection.execute(
                "UPDATE auth_grants SET actions=? WHERE grant_id=?", ("read,teleport", "g1")
            )
        found = _find(store)
        assert len(found) == 1
        assert found[0].actions == frozenset({Action.READ})
    finally:
        store.close()
