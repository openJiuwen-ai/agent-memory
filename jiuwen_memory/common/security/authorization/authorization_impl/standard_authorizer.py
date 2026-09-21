# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""标准 Authorizer：按 F05 §Authorization 决策顺序判定。

这是**唯一的生产授权实现**。取代 ``control.permission_impl.sqlite_permission_manager``
里那段与 SQLite 存储混在一起的判定逻辑：策略在这里，记录的存取在
:mod:`common.security.authorization.store` 后面。分开之后换存储后端不碰策略，
改策略不碰 SQL。

决策顺序即代码顺序（F05 §决策顺序）：

1. AuthContext 未过期；
2. actor 与请求上下文一致（由 PEP 保证，这里复核）；
3. 系统与管理面角色闸门；
4. actor 是否覆盖 target 的所有者范围；
5. Delegation 是否覆盖本次资源与 Action；
6. 显式 Grant 是否覆盖本次资源与 Action；
7. 默认拒绝。

顺序不是任意的：闸门（第 3 步）必须在所有放行规则之前，否则一条 Grant 就能把管理面
动作放给普通角色；owner（第 4 步）在委托与 Grant 之前，是因为它最常命中且不需查库。

第 3 步是**终局**判定——管理面动作走完角色校验就给出结论，不落到第 4-6 步（理由见
:meth:`StandardAuthorizer._management_plane`）。其余各步只会「拒绝或落到下一步」。
"""

from __future__ import annotations

from datetime import datetime

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.authorization.base import (
    AuthorizationDecision,
    AuthorizationProducer,
    Authorizer,
)
from jiuwen_memory.common.security.authorization.scope_rules import PrincipalPath, scope_covers
from jiuwen_memory.common.security.authorization.store import (
    DelegationStore,
    DelegationStoreProducer,
    GrantStore,
    GrantStoreProducer,
)
from jiuwen_memory.common.security.types import (
    MANAGEMENT_ACTIONS,
    ROLE_RANK,
    Action,
    AuthContext,
    AuthorizationEnvironment,
    Delegation,
    DenyReason,
    ResourceDescriptor,
    Role,
)
from jiuwen_memory.common.type_def.scope import Scope

# 管理面动作要求的**最低**角色。ADMIN 能管本 org 内的主体与 space；跨 org 的系统级
# 操作与审计校验要 ROOT。
_MINIMUM_ROLE: dict[Action, Role] = {
    Action.MANAGE_PRINCIPAL: Role.ADMIN,
    Action.MANAGE_SPACE: Role.ADMIN,
    Action.MANAGE_POLICY: Role.ADMIN,
    Action.READ_AUDIT: Role.ADMIN,
    Action.VERIFY_AUDIT: Role.ROOT,
    Action.ADMINISTER_SYSTEM: Role.ROOT,
}


def _principal_path(resource: ResourceDescriptor) -> PrincipalPath:
    """本次判定用哪种主体路径。

    取自 ``ResourceDescriptor.attributes``——而 descriptor 由 PEP 从 space policy
    这个真源构造（F05 §ResourceDescriptor）。值不合法时回落默认而不是抛：一个写错的
    space policy 不该让请求变成 500，回落到 ``user_agent`` 是两者中更严格的那个
    （agent 维在内层，覆盖面更小）。
    """
    raw = resource.attributes.get("principal_path", "")
    try:
        return PrincipalPath(raw)
    except ValueError:
        return PrincipalPath.USER_AGENT


def _delegate_is_machine_principal(delegation: Delegation) -> bool:
    """被委托方是否是 agent/service 这类**非人主体**（F05 §Delegation）。

    F05 把 Delegation 定义为「user 对 agent 或 service 的有限期代操作授权」。不校验
    主体种类的话，user -> user 的委托能通过判定，Delegation 就成了第二套 Grant：
    绕过 SHARE 动作的授权路径、绕过 grant/revoke 的管理面记录，而两者的撤销与治理
    接口完全不同。``Scope`` 目前只建模了 user 与 agent（service identity 待补），故
    判据是「delegate 必须带 agent 维」。

    委托方一侧的判据是「**不带 agent 维**」而非「必须带 user 维」。两者在标准链上
    等价，在空间链上不等价，取前者：空间级目标不带主体维（条目真源 scope 归一为空间
    级），而 :func:`delegation_binds` 要求 delegator 覆盖目标、覆盖判定的主体主维须
    精确相等，因此覆盖空间级目标的 delegator 只能是纯空间形状——与空间链上 Grant 的
    grantor 同一约定。若这里同时要求 delegator 带 user 维，两条判据在空间链上互斥，
    委托步的放行分支整体不可达（症状是 agent/service 的合法代操作在启用空间隔离后
    静默失效）。要拦的是「agent 再委托 agent」——委托关系自我复制后撤销追不上——这一条
    由 agent 维为空表达，不需要借 user 维非空来表达。

    空 ``Scope()`` 单独排除：它在覆盖判定里只覆盖空 scope，本不构成放行路径，但把
    「委托方是谁」这一项留空的记录不该算成立的委托。
    """
    if not delegation.delegate.agent:
        return False
    return delegation.delegator != Scope() and not delegation.delegator.agent


def _delegation_sources_for(authorizer: Authorizer) -> list[DelegationStore]:
    """读取实现层私有委托真源，不扩充已冻结的 Authorizer 公共契约。"""
    provider = getattr(authorizer, "_delegation_stores", None)
    if not callable(provider):
        return []
    return list(provider())


def by_delegation(
    auth: AuthContext,
    resource: ResourceDescriptor,
    *,
    action: Action | None,
    now: datetime,
    path: PrincipalPath,
    stores: list[DelegationStore],
) -> AuthorizationDecision:
    """第 5 步（标准链）/ 第 9 步（空间链）：按 ``delegation_id`` 回真源复核。

    模块级函数而非 ``StandardAuthorizer`` 私有方法：SpaceAwareAuthorizer 的空间链
    需要与标准链**同一份**委托判据（P1-3），挂在实现类上会让空间链要么复制一遍、
    要么反向依赖具体实现类。``stores`` 是判定时实际查询的全部 DelegationStore，
    由调用方从具体实现的私有 ``_delegation_stores()`` 接缝取——真源只有一个，且不
    扩充冻结的 :class:`Authorizer` 公共契约。

    ``action`` 是本次要求委托覆盖的动作；``None`` 表示该动作无可委托映射（如空间链
    的纯治理动作），一律按动作不在 allowlist 拒绝。带了 ``delegation_id`` 就**不
    再回落**到 Grant：调用方显式声明了「我在代操作」，委托不成立时静默改判成「那
    看看有没有 Grant」，会让一条失效委托的拒绝被另一条规则掩盖，审计里也就看不出
    委托失效过。
    """
    if action is None:
        return AuthorizationDecision.deny(DenyReason.DELEGATION_ACTION, "delegation_action")
    for store in stores:
        delegation = store.get(auth.delegation_id)
        if delegation is None:
            continue
        if not delegation.is_active(now=now):
            # 不存在、已撤销、已过期归同一个 reason：区分它们是委托枚举侧信道。
            return AuthorizationDecision.deny(DenyReason.DELEGATION_INVALID, "delegation_lookup")
        if not _delegate_is_machine_principal(delegation):
            return AuthorizationDecision.deny(DenyReason.DELEGATION_INVALID, "delegation_principal")
        if not delegation.permits(action):
            return AuthorizationDecision.deny(DenyReason.DELEGATION_ACTION, "delegation_action")
        if not delegation_binds(delegation, auth, resource, path=path):
            return AuthorizationDecision.deny(DenyReason.DELEGATION_INVALID, "delegation_binding")
        return AuthorizationDecision.allow("delegation")
    return AuthorizationDecision.deny(DenyReason.DELEGATION_INVALID, "delegation_lookup")


def delegation_binds(
    delegation: Delegation,
    auth: AuthContext,
    resource: ResourceDescriptor,
    *,
    path: PrincipalPath,
) -> bool:
    """委托的绑定条件是否全部成立。

    每一条都在回答同一个问题：**这条委托是发给此刻这个调用方、用于此刻这个资源
    的吗**。少任何一条，一条合法委托就能被别人捡去用。
    """
    actor = auth.actor
    if not scope_covers(delegation.delegate, actor, principal_path=path):
        # 拿别人的委托 id 来用。
        return False
    if not scope_covers(delegation.delegator, resource.scope, principal_path=path):
        # 委托方管不着这份资源——委托不能授出委托方自己都没有的范围。
        return False
    if delegation.allowed_spaces and resource.scope.space not in delegation.allowed_spaces:
        return False
    if delegation.bound_credential_id and delegation.bound_credential_id != auth.credential_id:
        # 绑定凭据后，换一把 key 的同一个 agent 用不了这条委托，泄露爆炸半径
        # 收敛在单把 key 上。
        return False
    return not (delegation.bound_session and delegation.bound_session != actor.session)


class StandardAuthorizer(Authorizer):
    """F05 决策顺序的实现。

    ``grant_store`` 与 ``delegation_store`` 是**必填**依赖，没有 ``None`` 形态。给它们
    可选形态就等于允许一个「跨主体访问一律拒绝」的降级模式静默存在，而那个模式与
    「存储配错了」在运行期长得完全一样。
    """

    def __init__(self, grant_store: GrantStore, delegation_store: DelegationStore) -> None:
        self._grants = grant_store
        self._delegations = delegation_store

    def health(self) -> None:
        self._grants.health()
        self._delegations.health()

    def management_grant_store(self) -> GrantStore:
        # PEP 的公共 grant/revoke 写这里：与 authorize 第 6 步 find_active 读同一实例，
        # 具名 YAML 令本 Authorizer 引用别的 Store 时，公共 grant 也写入同一 Store。
        return self._grants

    def _delegation_stores(self) -> list[DelegationStore]:
        # 空间链第 9 步经本列表读同一真源（P1-3）：委托在这里撤销，标准链与空间链
        # 的下一次判定同时看见。
        return [self._delegations]

    def authorize(
        self,
        *,
        auth: AuthContext,
        resource: ResourceDescriptor,
        environment: AuthorizationEnvironment,
    ) -> AuthorizationDecision:
        now = environment.now
        actor = auth.actor

        # -- 1. 上下文时效 ------------------------------------------------- #
        if auth.is_expired(now=now):
            return AuthorizationDecision.deny(DenyReason.EXPIRED_CONTEXT, "context_expiry")

        # -- 2. actor 一致性 ------------------------------------------------ #
        # actor 为空 Scope 意味着「没填内容的身份」，不是特权形态（F05 §授权不变量 1）。
        # 旧实现把它当 platform admin，那条线在这里彻底断掉。
        if actor == Scope():
            return AuthorizationDecision.deny(DenyReason.CONTEXT_MISMATCH, "empty_actor")

        # -- 3. 角色闸门 ---------------------------------------------------- #
        gate = self._management_plane(auth, resource)
        if gate is not None:
            return gate

        # ROOT 跨 org 全局放行——闸门之后才生效，故 ROOT 也走完了管理面的最低角色校验。
        if auth.role is Role.ROOT:
            return AuthorizationDecision.allow("root_role")

        # org 是硬边界，非 ROOT 一律不跨（F05 §Grant：Grant 不跨 org 生效）。放在
        # owner 判定之前：跨 org 的请求没有任何后续规则能救，早拒早给出准确 reason。
        if actor.org != resource.scope.org:
            return AuthorizationDecision.deny(DenyReason.CROSS_ORG, "org_boundary")

        path = _principal_path(resource)

        # -- 4. owner 覆盖 -------------------------------------------------- #
        if scope_covers(actor, resource.scope, principal_path=path):
            return AuthorizationDecision.allow("owner_cover")

        # -- 5. Delegation -------------------------------------------------- #
        if auth.delegation_id:
            return self._by_delegation(auth, resource, now=now, path=path)

        # -- 6. Grant ------------------------------------------------------- #
        if self._by_grant(actor, resource, now=now, path=path):
            return AuthorizationDecision.allow("grant")

        # -- 7. 默认拒绝 ---------------------------------------------------- #
        return AuthorizationDecision.deny(DenyReason.NOT_COVERED, "default_deny")

    # ------------------------------------------------------------------ #
    # 第 3 步：角色闸门
    # ------------------------------------------------------------------ #

    def _management_plane(
        self, auth: AuthContext, resource: ResourceDescriptor
    ) -> AuthorizationDecision | None:
        """管理面动作的**完整**判定；非管理面动作返回 ``None`` 落到下一步。

        「这是不是管理操作」由**封闭的 Action** 说了算，不由 ``resource_type`` 或
        「target 恰好是空 Scope」这类数据形状间接表达——后者是调用方能控制的。

        管理面在这里**判完就返回**，不落到 owner / Delegation / Grant：

        - 往下走会**永远拒**。管理别人的主体、别人的 space，目标本就不在 ADMIN
          自己的 scope 内，owner 规则必拒；
        - 往下走还会**开一道后门**。若某天 Grant 兜住了这条路径，就等于「能写 Grant
          的人可以自助提权到管理面」。管理面的准入依据只有一个：服务端 role。

        target 的 org 为空表示**系统级资源**（全局治理策略、跨 org 审计），只有 ROOT
        能碰；带 org 的管理面资源（space、主体）ADMIN 可管，但止于本 org。
        """
        action = resource.action
        if action not in MANAGEMENT_ACTIONS:
            return None
        required = _MINIMUM_ROLE[action]
        if ROLE_RANK[auth.role] < ROLE_RANK[required]:
            return AuthorizationDecision.deny(DenyReason.ROLE_REQUIRED, "role_gate")
        if not resource.scope.org:
            # 系统级资源：全局治理策略、跨 org 审计查询没有 org 归属，ADMIN 的本 org
            # 管辖覆盖不到它们，只有 ROOT 能碰。显式判一次而不是让它掉进下面那句
            # 「org 不等」——那句会给出 CROSS_ORG，把运维引向「是不是 org 配错了」，
            # 而事实是「这个角色不够」。reason code 是审计与告警的匹配依据，得准。
            if auth.role is not Role.ROOT:
                return AuthorizationDecision.deny(DenyReason.ROLE_REQUIRED, "role_gate_system")
            return AuthorizationDecision.allow("role_gate")
        if auth.role is not Role.ROOT and auth.actor.org != resource.scope.org:
            # ADMIN 的管辖范围止于本 org（F05 §Role：ADMIN 不可跨 org）。
            return AuthorizationDecision.deny(DenyReason.CROSS_ORG, "role_gate_org")
        return AuthorizationDecision.allow("role_gate")

    # ------------------------------------------------------------------ #
    # 第 5 步：Delegation（判据本体在模块级 by_delegation/delegation_binds，
    # 与空间链共用同一份）
    # ------------------------------------------------------------------ #

    def _by_delegation(
        self,
        auth: AuthContext,
        resource: ResourceDescriptor,
        *,
        now: datetime,
        path: PrincipalPath,
    ) -> AuthorizationDecision:
        """按 ``delegation_id`` 回真源复核（F05 §Delegation）。

        ``auth`` 里只有一个 id，委托的**内容**一律从 Store 读——认证层产出的
        ``delegation_id`` 证明的是「这个 id 出现在一次已认证的请求里」，不是「这条委托
        此刻仍然有效且覆盖本次动作」。语义见模块级 :func:`by_delegation`。
        """
        return by_delegation(
            auth,
            resource,
            action=resource.action,
            now=now,
            path=path,
            stores=self._delegation_stores(),
        )

    # ------------------------------------------------------------------ #
    # 第 6 步：Grant
    # ------------------------------------------------------------------ #

    def _by_grant(
        self,
        actor: Scope,
        resource: ResourceDescriptor,
        *,
        now: datetime,
        path: PrincipalPath,
    ) -> bool:
        """是否存在一条覆盖本次判定的有效 Grant。

        两侧都要覆盖：grantee 覆盖 actor（这条授权是给他的），grantor 覆盖 target
        （授权方管得着这份资源）。只查一侧就是把「谁被授权」和「授权了什么」拆开，
        任意一半都能被另一半的宽松形状放大。
        """
        grants = self._grants.find_active(
            grantee=actor,
            grantor_org=resource.scope.org,
            action=resource.action,
            now=now,
        )
        for grant in grants:
            if not grant.is_active(now=now):
                # Store 契约要求已滤，这里再确认一次：时效判定必须用本次的同一个 now，
                # 而 Store 用的是入参 now 还是自己取的，跨实现无法保证。
                continue
            if scope_covers(grant.grantee, actor, principal_path=path) and scope_covers(
                grant.grantor, resource.scope, principal_path=path
            ):
                return True
        return False


@AuthorizationProducer.register("standard")
def _build(config) -> StandardAuthorizer:
    """装配标准 Authorizer。

    两个 Store 都**无默认实现**：给它们默认会让「忘了配授权存储」静默变成某种可用
    配置（F05 §装配不变量 6）。要什么后端就在 YAML 里写出来。
    """
    grant_store = GrantStoreProducer.dep(config, "grant_store")
    delegation_store = DelegationStoreProducer.dep(config, "delegation_store")
    if not isinstance(grant_store, GrantStore):
        raise ValidationError("authorizer.standard params.grant_store 必须是 GrantStore")
    if not isinstance(delegation_store, DelegationStore):
        raise ValidationError("authorizer.standard params.delegation_store 必须是 DelegationStore")
    return StandardAuthorizer(grant_store=grant_store, delegation_store=delegation_store)
