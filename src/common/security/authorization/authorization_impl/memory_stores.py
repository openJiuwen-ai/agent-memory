"""进程内 :class:`GrantStore` 与 :class:`DelegationStore`。

定位与 ``InMemoryKeyStore`` 一致：单进程装配、测试与 DEV 用。**进程重启即全部丢失**
——生产要 SQLite 后端（``sqlite_authorization_store``）。

注册名是 ``memory``（描述存储后端），与 ``key_store`` 的 ``memory`` 惯例一致。

两个 Store 放同一模块：它们的记录形态、锁策略和撤销语义是对称的，分成两个文件会
让「Grant 改了撤销语义、Delegation 没跟上」这类分叉更容易发生。它们仍是两个**类**、
两个 Producer——共享文件不等于共享类型。
"""

from __future__ import annotations

import threading
from datetime import datetime

from common.security.authorization.store import (
    DelegationStore,
    DelegationStoreProducer,
    GrantStore,
    GrantStoreProducer,
)
from common.security.types import Action, Delegation, Grant
from common.type_def.scope import Scope


class InMemoryGrantStore(GrantStore):
    """进程内授权表。

    按 ``grant_id`` 索引：撤销要按 id 定位，而 grantor/grantee/action 的组合会重复
    （同一对主体可以有多条不同有效期的授权）。
    """

    def __init__(self) -> None:
        self._grants: dict[str, Grant] = {}
        self._lock = threading.Lock()

    def add(self, grant: Grant) -> None:
        with self._lock:
            existing = self._grants.get(grant.grant_id)
            if existing is not None and existing.revoked:
                # 撤销单调：同 id 的写入不得把 revoked 翻回 False。队列重投或网络重试
                # 会把撤销前的那份创建请求再送一次，无条件覆盖等于给攻击者一条「重放
                # 旧报文即可复活权限」的路径。SQLite 实现的 upsert 不动 revoked_at，
                # 语义一致。
                return
            self._grants[grant.grant_id] = grant

    def revoke(self, grant_id: str) -> None:
        with self._lock:
            existing = self._grants.get(grant_id)
            if existing is None:
                return  # 幂等
            # 软撤销：置标记而不是删记录。硬删除会让「这条权限什么时候没的」在审计里
            # 断线，而权限消失的时刻恰恰是事故复盘要问的第一个问题。
            self._grants[grant_id] = Grant(
                grant_id=existing.grant_id,
                grantor=existing.grantor,
                grantee=existing.grantee,
                actions=existing.actions,
                expires_at=existing.expires_at,
                revoked=True,
            )

    def find_active(
        self,
        *,
        grantee: Scope,
        grantor_org: str,
        action: Action,
        now: datetime,
    ) -> list[Grant]:
        with self._lock:
            candidates = list(self._grants.values())
        # 时效与撤销在存储层就滤掉（契约要求）。scope 覆盖不在这里判——那是策略，
        # 归 Authorizer；这里只按能索引的维度收窄候选集。
        active = []
        for grant in candidates:
            if action not in grant.actions:
                continue
            if grant.grantor.org != grantor_org or grant.grantee.org != grantee.org:
                continue
            if grant.is_active(now=now):
                active.append(grant)
        return active

    def health(self) -> None:
        return None


class InMemoryDelegationStore(DelegationStore):
    """进程内委托表。"""

    def __init__(self) -> None:
        self._delegations: dict[str, Delegation] = {}
        self._lock = threading.Lock()

    def add(self, delegation: Delegation) -> None:
        with self._lock:
            existing = self._delegations.get(delegation.delegation_id)
            if existing is not None and existing.revoked:
                return  # 撤销单调，同 InMemoryGrantStore.add
            self._delegations[delegation.delegation_id] = delegation

    def revoke(self, delegation_id: str) -> None:
        with self._lock:
            existing = self._delegations.get(delegation_id)
            if existing is None:
                return  # 幂等
            self._delegations[delegation_id] = Delegation(
                delegation_id=existing.delegation_id,
                delegator=existing.delegator,
                delegate=existing.delegate,
                actions=existing.actions,
                expires_at=existing.expires_at,
                not_before=existing.not_before,
                revoked=True,
                allowed_spaces=existing.allowed_spaces,
                bound_credential_id=existing.bound_credential_id,
                bound_session=existing.bound_session,
            )

    def get(self, delegation_id: str) -> Delegation | None:
        # 空 id 直接返回 None：``AuthContext.delegation_id`` 的默认值是空串，让它
        # 去查表意味着一条 id 为空的记录能被任何未声明委托的请求命中。
        if not delegation_id:
            return None
        with self._lock:
            return self._delegations.get(delegation_id)

    def health(self) -> None:
        return None


@GrantStoreProducer.register("memory")
def _build_grant_store(config) -> InMemoryGrantStore:
    return InMemoryGrantStore()


@DelegationStoreProducer.register("memory")
def _build_delegation_store(config) -> InMemoryDelegationStore:
    return InMemoryDelegationStore()
