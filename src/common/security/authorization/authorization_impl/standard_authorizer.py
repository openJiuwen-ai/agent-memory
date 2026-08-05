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

from common.errors import ValidationError
from common.security.authorization.base import (
    AuthorizationDecision,
    AuthorizationProducer,
    Authorizer,
)
from common.security.authorization.scope_rules import PrincipalPath, scope_covers
from common.security.authorization.store import (
    DelegationStore,
    DelegationStoreProducer,
    GrantStore,
    GrantStoreProducer,
)
from common.security.types import (
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
from common.type_def.scope import Scope

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
    # 第 5 步：Delegation
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
        此刻仍然有效且覆盖本次动作」。

        带了 delegation_id 就**不再回落**到 Grant：调用方显式声明了「我在代操作」，
        委托不成立时静默改判成「那看看有没有 Grant」，会让一条失效委托的拒绝被另一条
        规则掩盖，审计里也就看不出委托失效过。
        """
        delegation = self._delegations.get(auth.delegation_id)
        if delegation is None or not delegation.is_active(now=now):
            # 不存在、已撤销、已过期归同一个 reason：区分它们是委托枚举侧信道。
            return AuthorizationDecision.deny(DenyReason.DELEGATION_INVALID, "delegation_lookup")
        if not delegation.permits(resource.action):
            return AuthorizationDecision.deny(DenyReason.DELEGATION_ACTION, "delegation_action")
        if not self._delegation_binds(delegation, auth, resource, path=path):
            return AuthorizationDecision.deny(DenyReason.DELEGATION_INVALID, "delegation_binding")
        return AuthorizationDecision.allow("delegation")

    def _delegation_binds(
        self,
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
        raise ValidationError(
            "authorizer.standard params.delegation_store 必须是 DelegationStore"
        )
    return StandardAuthorizer(grant_store=grant_store, delegation_store=delegation_store)
