# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""空间感知 Authorizer：F07 两轴判定在唯一 PDP 上的宿主（F11 决策 2）。

取代 ``control.permission_impl.space_aware_permission_manager`` 在生产路径上的位置：
PEP 不再调 ``PermissionManager.decide``，空间级入口的判定改由本实现承担。判据主体
仍是 :mod:`common.security.space_decision` 的纯函数，本模块只做三件事：从
``ResourceDescriptor.attributes`` 还原判定输入、经 delegate 的授权真源查显式授权、
把 ``DecisionOutcome`` 折算成 ``AuthorizationDecision``。

**组合而非继承 delegate**：本实现不复制标准链的任何一段。非空间级入口（未登记入口
与组织级入口）整体回落 delegate——组织级入口由 delegate 的管理面角色闸门终局裁决，
未登记入口按标准链判定，行为与空间判定实装前一致。

空间级入口的链序是标准链前缀 + F07 判据：时效、空身份、管理面闸门、ROOT、org 硬
边界之后，接归属对比、主体覆盖、代操作委托、归属主体档、两轴求值。委托按
``delegation_id`` 经 delegate 的 DelegationStore 回真源复核；具名用户委托另验证
PEP 的委托人事实投影及其当前内容权（F12），纯空间委托复用标准链 by_delegation。
结论折算成 DecisionOutcome 后传入 decide_projected 的第 9 步终局裁决（F07 十步）。

显式授权经 delegate 的 :meth:`Authorizer.management_grant_stores` 查询：管理面
grant/revoke 写入的 Store 与空间链判定读取的 Store 是同一批实例，撤销在两者同时
生效——这是「Authorizer 是唯一 PDP、GrantStore 是唯一授权状态真源」（不变量
23 / 27）在空间链上的落实。委托真源经实现层私有接缝透传，不扩充冻结的
``Authorizer`` 公共契约；撤销在标准链与空间链的下一次判定同时可见。
"""

from __future__ import annotations

from datetime import datetime

from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security._delegation_binding import (
    _PROJECTION_PREFIX,
    _bound_record,
    _effective_principal,
)
from jiuwen_memory.common.security.authorization.authorization_impl.standard_authorizer import (
    _delegation_sources_for,
    by_delegation,
)
from jiuwen_memory.common.security.authorization.base import (
    AuthorizationDecision,
    AuthorizationProducer,
    Authorizer,
)
from jiuwen_memory.common.security.authorization.scope_rules import (
    PrincipalPath,
    scope_covers,
)
from jiuwen_memory.common.security.principal import AUTHOR_PRINCIPAL
from jiuwen_memory.common.security.space_decision import (
    ATTR_PRINCIPAL_PATH,
    ATTR_SPACE_AXIS,
    ATTR_SPACE_ENTRY,
    DecisionOutcome,
    axes_for,
    decide_projected,
    on_behalf_paths_apply,
    projection_from_attributes,
    resolve_space_action,
)
from jiuwen_memory.common.security.space_decision import DenyReason as SpaceDenyReason
from jiuwen_memory.common.security.space_roles import (
    ENTRY_RULES,
    SpaceAction,
    SpaceAxis,
)
from jiuwen_memory.common.security.types import (
    MANAGEMENT_ACTIONS,
    Action,
    AuthContext,
    AuthorizationEnvironment,
    DenyReason,
    ResourceDescriptor,
    Role,
)
from jiuwen_memory.common.type_def.scope import Scope

# 空间动作枚举到授权记录动作的映射。显式授权只在内容轴参与求值，而内容轴用到的五个
# 动作在安全层封闭枚举中都有同名项；治理轴独有的 REVOKE_SHARE 与三个组织级动作不
# 参与，因此不在表内，查授权记录时按「无对应动作」处置。
_GRANT_ACTIONS: dict[SpaceAction, Action] = {
    SpaceAction.READ: Action.READ,
    SpaceAction.WRITE: Action.WRITE,
    SpaceAction.UPDATE: Action.UPDATE,
    SpaceAction.DELETE: Action.DELETE,
    SpaceAction.SHARE: Action.SHARE,
}


def _path_of(resource: ResourceDescriptor) -> PrincipalPath:
    """本次判定用哪种主体路径，取自 space policy 写入的属性通道。

    值不合法时回落默认而不是抛：一个写坏的 space policy 不该让请求变成 500，回落到
    ``user_agent`` 是两者中更严格的那个（agent 维在内层，覆盖面更小）。
    """
    raw = resource.attributes.get(ATTR_PRINCIPAL_PATH, "")
    try:
        return PrincipalPath(raw) if raw else PrincipalPath.USER_AGENT
    except ValueError:
        return PrincipalPath.USER_AGENT


class SpaceAwareAuthorizer(Authorizer):
    """两轴角色、归属对比与归属主体档的判定实现。

    ``delegate`` 是**必填**依赖：标准链回落由它承担，空间链复用共同判据。
    空间链上的显式授权和委托也经 delegate 的
    GrantStore 查询，保证管理面与判定面读同一真源。
    """

    def __init__(self, delegate: Authorizer) -> None:
        self._delegate = delegate

    # ------------------------------------------------------------------ #
    # 契约透传
    # ------------------------------------------------------------------ #

    def requires_space_facts(self) -> bool:
        """空间级判据要成员表与归属登记，由鉴权点一次读取后折算成属性投影传入。

        capability 方法而非契约基类方法：Authorizer 契约冻结，PEP 经 ``getattr``
        探测本能力，未声明者按不需要空间事实处置。
        """
        return True

    def is_test_only(self) -> bool:
        return self._delegate.is_test_only()

    def health(self) -> None:
        self._delegate.health()

    def management_grant_store(self):
        # 管理写真源即 delegate 的真源：grant/revoke 写入的 Store 与空间链第 6 步
        # find_active 读的是同一实例，撤销在两侧同时生效。
        return self._delegate.management_grant_store()

    def management_grant_stores(self):
        return self._delegate.management_grant_stores()

    def _delegation_stores(self):
        """透传 delegate 的实现层私有委托真源。"""
        return _delegation_sources_for(self._delegate)

    def routing_fields(self) -> tuple[str, ...]:
        # 非空间级入口按 delegate 的路由字段路由；空间级入口不按资源属性路由，
        # 由 PEP 单独判定（见 pep_ops._routing_fields_for）。
        return self._delegate.routing_fields()

    # ------------------------------------------------------------------ #
    # 判定
    # ------------------------------------------------------------------ #

    def authorize(
        self,
        *,
        auth: AuthContext,
        resource: ResourceDescriptor,
        environment: AuthorizationEnvironment,
    ) -> AuthorizationDecision:
        entry = str(resource.attributes.get(ATTR_SPACE_ENTRY, "")).strip()
        rule = ENTRY_RULES.get(entry)
        if rule is None or rule.axis is SpaceAxis.ORG:
            # 未登记入口与组织级入口不落两轴求值：前者尚未纳入空间级判定，后者由
            # delegate 的管理面角色闸门终局裁决。两者整体回落标准链，行为与空间判定
            # 实装前一致，不因判定实现的装配而收紧。
            return self._delegate.authorize(auth=auth, resource=resource, environment=environment)

        now = environment.now
        actor = auth.actor

        # -- 标准链前缀：与 StandardAuthorizer.authorize 逐段对齐 ---------------- #
        if auth.is_expired(now=now):
            return AuthorizationDecision.deny(DenyReason.EXPIRED_CONTEXT, "context_expiry")

        if actor == Scope():
            return AuthorizationDecision.deny(DenyReason.CONTEXT_MISMATCH, "empty_actor")

        if resource.action in MANAGEMENT_ACTIONS:
            # 空间级入口不应携带管理面动作；携带时按不变量 26 的固定次序交回角色
            # 闸门终局裁决，不让空间链绕过管理面准入。
            return self._delegate.authorize(auth=auth, resource=resource, environment=environment)

        if auth.role is Role.ROOT:
            return AuthorizationDecision.allow("root_role")

        if actor.org != resource.scope.org:
            return AuthorizationDecision.deny(DenyReason.CROSS_ORG, "org_boundary")

        # -- F07 判据：投影还原 + 委托复核 + 两轴求值 --------------------------- #
        projection = projection_from_attributes(resource.attributes)
        space_action = resolve_space_action(rule.action, resource.attributes)
        author_principal = resource.attributes.get(AUTHOR_PRINCIPAL, "") or None
        path = _path_of(resource)
        covered = scope_covers(actor, resource.scope, principal_path=path)
        requested = str(resource.attributes.get(ATTR_SPACE_AXIS, "")).strip()
        axes = axes_for(rule.axis, requested)

        # 委托只在「代人操作」适用的轴与入口参与（判据见 on_behalf_paths_apply，与第 7
        # 步同一份）。不适用时不回真源查询：鉴权路径上不产生与结论无关的存储访问。
        delegation = None
        if any(on_behalf_paths_apply(entry, axis) for axis in axes):
            delegation = self._delegation_outcome(
                auth, resource, space_action=space_action, now=now, path=path
            )

        # 固定判定链要求 Delegation 先于 Grant，且声明委托后该步终局。不能先读 GrantStore
        # 再复核委托：GrantStore 故障会把一条本可由有效委托终局裁决的请求错误变成 503，
        # 也让失效委托产生与结论无关的授权真源访问。
        granted = (
            frozenset()
            if delegation is not None
            else self._granted_actions(
                actor, resource, space_action=space_action, now=now, path=path
            )
        )

        outcome = DecisionOutcome(allowed=False, rule="no_axis_evaluated")
        for axis in axes:
            outcome = decide_projected(
                actor=actor,
                target=resource.scope,
                projection=projection,
                entry=entry,
                action=space_action,
                axis=axis,
                author_principal=author_principal,
                principal_path=path.value,
                granted_actions=granted,
                scope_covered=covered,
                own_actions_apply=rule.own_actions_apply,
                delegation=delegation,
            )
            if outcome.allowed:
                return self._decision(outcome)
        return self._decision(outcome)

    @staticmethod
    def _decision(outcome: DecisionOutcome) -> AuthorizationDecision:
        """把空间判据结论折算成 Authorizer 契约的结论类型。

        两套 ``DenyReason`` 的取值子集相同（``context_mismatch`` / ``not_covered`` /
        ``delegation_invalid`` / ``delegation_action``），按值转换；允许侧不带轴——
        鉴权点需要通过的轴时逐轴调用本实现，从调用序列得知（``AuthorizationDecision``
        的字段面已冻结）。
        """
        if outcome.allowed:
            return AuthorizationDecision.allow(outcome.rule)
        reason = (
            DenyReason(outcome.reason.value)
            if outcome.reason is not None
            else DenyReason.NOT_COVERED
        )
        return AuthorizationDecision.deny(reason, outcome.rule)

    def _delegation_outcome(
        self,
        auth: AuthContext,
        resource: ResourceDescriptor,
        *,
        space_action: SpaceAction,
        now: datetime,
        path: PrincipalPath,
    ) -> DecisionOutcome | None:
        """第 9 步代操作委托的复核结论，回 delegate 的委托真源。

        ``auth`` 未声明 ``delegation_id`` 时返回 ``None``，本步不参与；声明了就把
        ``by_delegation`` 的结论折算成空间判据的 ``DecisionOutcome``——允许侧 rule
        取 ``"delegation"``，拒绝侧按值取两套 DenyReason 的公共子集。

        调用方先按 :func:`on_behalf_paths_apply` 过滤入口与轴，因此本方法只在委托
        确实可能参与判定时被调用。

        **终局语义与标准链一致**：委托适用且已声明时就不再回落成员记录/Grant，委托
        失效以审计可见的方式拒绝，而不是静默改判成更宽松的其它来源。
        """
        if not auth.delegation_id:
            return None
        sources = _delegation_sources_for(self._delegate)
        records = [s.get(auth.delegation_id) for s in sources]
        if (
            auth.actor.agent
            and not auth.actor.user
            and any(r is not None and r.delegator.user for r in records)
        ):
            try:
                record = _bound_record(
                    auth, list(sources), now=now, action=resource.action, target=resource.scope
                )
                effective = _effective_principal(auth, record)
                if resource.attributes.get(_PROJECTION_PREFIX + "user") != effective.user:
                    raise PermissionDeniedError("delegation_invalid")
            except PermissionDeniedError:
                return DecisionOutcome(
                    allowed=False, rule="delegation", reason=SpaceDenyReason.DELEGATION_INVALID
                )
            entry = str(resource.attributes.get(ATTR_SPACE_ENTRY, ""))
            source_inputs = dict(
                actor=effective,
                target=resource.scope,
                projection=projection_from_attributes(
                    {
                        key.removeprefix(_PROJECTION_PREFIX): value
                        for key, value in resource.attributes.items()
                        if key.startswith(_PROJECTION_PREFIX)
                    }
                ),
                entry=entry,
                action=space_action,
                axis=SpaceAxis.CONTENT,
                author_principal=resource.attributes.get(AUTHOR_PRINCIPAL) or None,
                principal_path=path.value,
                scope_covered=False,
                own_actions_apply=ENTRY_RULES[entry].own_actions_apply,
                delegation=None,
            )
            source = decide_projected(**source_inputs)
            # 委托人已有归属/成员权即终局，不因无关 GrantStore 故障破坏有效委托。
            if not source.allowed:
                granted = self._granted_actions(
                    effective, resource, space_action=space_action, now=now, path=path
                )
                if granted:
                    source = decide_projected(**source_inputs, granted_actions=granted)
            return DecisionOutcome(
                allowed=source.allowed,
                rule="delegation",
                reason=None if source.allowed else SpaceDenyReason.NOT_COVERED,
            )
        decision = by_delegation(
            auth,
            resource,
            action=_GRANT_ACTIONS.get(space_action),
            now=now,
            path=path,
            stores=sources,
        )
        if decision.allowed:
            return DecisionOutcome(allowed=True, rule="delegation")
        return DecisionOutcome(
            allowed=False,
            rule=decision.rule,
            reason=SpaceDenyReason(decision.reason.value),
        )

    def _granted_actions(
        self,
        actor: Scope,
        resource: ResourceDescriptor,
        *,
        space_action: SpaceAction,
        now: datetime,
        path: PrincipalPath,
    ) -> frozenset[SpaceAction]:
        """显式授权命中的动作集合，查 delegate 的授权真源。

        只探测本次要求的那个动作：两轴求值最终只判该动作是否落在集合内，逐个探测其余
        动作会在鉴权路径上产生与结论无关的存储访问。

        双侧覆盖与 ``StandardAuthorizer._by_grant`` 同一模式：grantee 覆盖 actor
        （这条授权是给他的），grantor 覆盖 target（授权方管得着这份资源）。
        """
        grant_action = _GRANT_ACTIONS.get(space_action)
        if grant_action is None:
            return frozenset()
        if actor.org != resource.scope.org:
            return frozenset()
        for store in self._delegate.management_grant_stores():
            for grant in store.find_active(
                grantee=actor,
                grantor_org=resource.scope.org,
                action=grant_action,
                now=now,
            ):
                if not grant.is_active(now=now):
                    continue
                if scope_covers(grant.grantee, actor, principal_path=path) and scope_covers(
                    grant.grantor, resource.scope, principal_path=path
                ):
                    return frozenset({space_action})
        return frozenset()


@AuthorizationProducer.register("space_aware")
def _build(config) -> SpaceAwareAuthorizer:
    """装配空间感知 Authorizer。

    ``delegate`` 必须显式指向一个具名 authorizer 且不得自指：空间链回落标准链的
    入口与授权真源都来自它，缺省或自指会让回落路径静默丢失。
    """
    delegate_name = str(config.get("delegate", "")).strip()
    if not delegate_name:
        raise ValidationError("authorizer.space_aware params.delegate 必须指向一个具名 authorizer")
    if delegate_name == config.name:
        raise ValidationError("authorizer.space_aware params.delegate 不能指向 space_aware 自身")
    delegate = AuthorizationProducer.build_named(delegate_name, config.ctx)
    if not isinstance(delegate, Authorizer):
        raise ValidationError(f"authorizer.space_aware 的 {delegate_name!r} 必须是 Authorizer")
    return SpaceAwareAuthorizer(delegate=delegate)
