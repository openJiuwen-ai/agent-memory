# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""PEP helpers: space facts, authorize, audit, check_write."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security import principal
from jiuwen_memory.common.security.request_context import get_request_id
from jiuwen_memory.common.security.space_decision import (
    ATTR_PRINCIPAL_PATH,
    ATTR_SPACE_ACTION,
    ATTR_SPACE_AXIS,
    ATTR_SPACE_ENTRY,
    content_grade,
    exceeds_governance_ceiling,
    governance_grade,
    raises_own_grade,
)
from jiuwen_memory.common.security.space_roles import (
    SpaceAction,
    SpaceAuthorizationFacts,
    SpaceAxis,
)
from jiuwen_memory.common.security.types import Action, Grant, RequestSecurityContext
from jiuwen_memory.common.type_def import (
    AuditEvent,
    MetadataValueType,
    Scope,
)
from jiuwen_memory.construction.router import (
    RouteContext,
)
from jiuwen_memory.control import collective
from jiuwen_memory.control.types import (
    PermissionContext,
    PrincipalPath,
    SpaceFacts,
    SpaceInfo,
    SpaceMember,
    SpacePatch,
    SpaceSpec,
    SpaceStatus,
)

from .local_support import (
    _GRANT_ACTION_BACK,
    _ROOT,
    _context_detail,
    _is_space_level_entry,
    _is_status_only,
    _missing_required_space,
    _policy_bool,
    _project_space_facts,
    _reject_non_scalar_metadata,
    _space_scope,
    _space_target_id,
    _write_permission_context,
)

logger = get_logger("jiuwen_memory.api.memory_api_impl.local_memory_api")


class PepOpsMixin:
    """PEP helpers: space facts, authorize, audit, check_write."""

    def _can_write_space(
        self, identity: Scope, target: Scope, *, require_writable_state: bool = False
    ) -> bool:
        """候选空间的写权判定，走与单空间写入入口同一个判定链。

        **``require_writable_state`` 为真时连同生命周期状态一起判。** 鉴权点的状态校验排在
        授权通过之后、按选定落点执行；候选筛选只判权则一个已冻结或已归档的空间仍会被选中，
        随后在写入处抛「空间不可写」，而此时可写的 fallback 已不再被考虑——表现是整次写入
        失败，而不是落到兜底空间。fallback 自身不加这项要求，理由见
        :func:`~control.collective.write_targets.plan_write_targets`。

        状态与权限取同一份快照：状态取 :meth:`_apply_space_policy_context` 连带返回的那份
        事实，不另读一次。

        逐空间的拒绝不落审计：一次写入对无权空间产生 M 条拒绝记录，审计价值低于噪声成本；
        整次调用仍记一条。与 :meth:`_readable_spaces` 同形。
        """
        context, facts = self._apply_space_policy_context(
            target,
            _write_permission_context(target, None, None),
            entry="add",
        )
        try:
            outcome = self._perm.decide(identity, target, Action.WRITE, context=context)
            if not outcome.allowed:
                return False
            if require_writable_state:
                self._ensure_space_state_allows(
                    target, Action.WRITE, "add", info=facts.info if facts is not None else None
                )
        except (PermissionDeniedError, BackendError, NotFoundError, ValidationError):
            return False
        return True

    def _ensure_fallback_space(self, identity: Scope, org: str, space: str) -> None:
        """fallback 空间不存在时按调用方身份自动创建并登记归属（策略开关，默认开）。

        只对 fallback 这一处做，且只处理「不存在」：

        - fallback 空间名由调用方**自己的身份**渲染而来，别的主体渲染不出它，因此没有
          抢占面，归属该登记给谁也是确定的——就是调用方本人。调用方传入的 ``scope``
          指向不存在的空间时不做同样的事：那个空间名是调用方给的字符串，而内核只有
          「坐标 → 空间名」的渲染方向，反解不出它该归谁；登记给写入者即先写入者占有
          该空间名。
        - 空间存在但调用方无写权（归档、被移出归属登记）时不创建，照常拒绝。自动创建
          处理的是「还没开通」，不是「不让你写」。

        创建走空间管理器而不是 ``create_space`` 入口：这是内核代行的动作，不是调用方
        发起的治理动作，不该按调用方身份过一次建空间的闸门。仍落一条审计事件，使自动
        创建在审计里可归因。

        并发下两个请求可能同时创建同一个空间，后到的得 :class:`ConflictError`——按已存在
        处置即可，随后的判权会得出相同结论。
        """
        if not space or self._space_info_if_exists(_space_scope(org, space)) is not None:
            return
        if not _policy_bool(self._policy, "space.auto_create_fallback", default=True):
            return
        owner = principal.owner_entry_of(identity, org, space)
        if owner is None:
            return
        try:
            self._space.create(SpaceSpec(org=org, space=space, owner=owner))
        except ConflictError:
            return
        # 建空间要下发事实失效：建之前任何一次读取都会装填一份「空间不存在」的事实，
        # 不清则新空间在一个 TTL 内判定无归属，归属主体本人也写不进去。
        self._invalidate_space_facts(org, space)
        self._log(
            identity,
            "create_space",
            _space_target_id(org, space),
            target_scope=_space_scope(org, space),
            detail={"entry": "auto_create_fallback", "owner": owner.user or owner.agent},
        )

    def _route_context(self, identity: Scope, coords: dict[str, str] | None) -> RouteContext:
        """装配判定上下文：坐标以身份覆盖内核三项，候选空间按写权取交。

        fallback 空间不可写时 :func:`~control.collective.write_targets.plan_write_targets`
        返回 ``fallback=None``，本方法据此整体拒绝写入——判不准时无处可落，静默落到别处
        等于把兜底落点交给判定实现决定。权限异常在本层抛出，控制层只出集合运算结果。
        """
        table = self._route_table
        resolved = principal.kernel_coords(coords, identity)
        fallback_space = table.naming.fallback_space(resolved)
        self._ensure_fallback_space(identity, identity.org, fallback_space)
        targets = collective.plan_write_targets(
            identity.org,
            resolved,
            table.naming,
            can_write=lambda scope, require_state: self._can_write_space(
                identity, scope, require_writable_state=require_state
            ),
            limit=self._space_fanout_limit(),
        )
        if targets.fallback is None:
            # fallback 空间由调用方自己的身份渲染而来，指出它的状态不构成资源枚举侧信道——
            # 调用方本来就知道自己是谁。其余候选空间不给同等提示：那会让任意主体可以逐个
            # 试探组织内有哪些空间存在。控制层只出集合运算结果，权限异常在鉴权点抛出。
            raise PermissionDeniedError(
                f"fallback space {identity.org}/{fallback_space!r} is not writable: "
                f"nowhere to fall back to. 该空间可能尚未注册——空间须先经 create_space "
                f"创建才能写入，用户主空间由开通服务在用户开通时预建"
            )
        return RouteContext(
            coords=resolved,
            candidates=targets.candidates,
            fallback=targets.fallback,
            classes=table.classes,
            narrow_dims=table.narrow_dims,
        )

    def _space_info_if_exists(self, scope: Scope) -> SpaceInfo | None:
        if not scope.org or not scope.space:
            return None
        try:
            return self._space.get(scope.org, scope.space)
        except NotFoundError:
            return None

    def _ensure_space_writable(self, scope: Scope) -> None:
        """写入前置校验。保留原名与原判据，供未装配空间级判定的部署继续使用。

        装配空间级判定时本方法不执行：这四处写路径的入口都已登记在 ``ENTRY_RULES`` 内，
        鉴权时已走过一次 :meth:`_ensure_space_state_allows`，两者对写动作的结论一致。
        保留而不删除，是因为未装配的部署里它是唯一的状态校验点。
        """
        if self._needs_space_facts():
            return
        info = self._space_info_if_exists(scope)
        if info is None:
            return
        if info.status != SpaceStatus.ACTIVE:
            raise ValidationError(
                f"space is not writable: {info.org}/{info.space} status={info.status.value}"
            )

    def _ensure_space_state_allows(
        self,
        target: Scope,
        action: Action,
        entry: str,
        *,
        info: SpaceInfo | None = None,
        patch: SpacePatch | None = None,
    ) -> None:
        """空间生命周期状态校验（F07「空间状态校验」）。

        排在授权通过之后、入口返回之前。位置是刻意的：排在授权之前，无权调用方也能凭
        错误类型判断出该空间处于冻结或归档，与「不泄露空间是否存在」的方向相反；排在
        之后不削弱约束——归属主体与成员都要先过授权再撞上状态校验。

        | 状态 | 规则 |
        |---|---|
        | ``ACTIVE`` 或空间不存在 | 通过 |
        | ``DELETING`` / ``DELETED`` | 除 ``delete_space`` 外全部拒绝，以便重试删完 |
        | ``FROZEN`` / ``ARCHIVED`` | 读动作放行；仅改状态的那次变更放行；其余拒绝 |

        「仅改状态」按 ``patch`` 的内容判定，不按入口名：``update_space`` 同时能改
        ``policy`` 与 ``principal_path``，而这两项都是判定依据，只看入口名等于允许在
        冻结态改写判定依据，冻结语义随之不成立。``archive_space`` 无 patch，其变更按
        定义只涉及状态。

        空间元数据由调用方传入：状态与判定事实同属一次读取，另读一次会使两处判据基于
        不同快照。未传入时回落到独立点读，供未接入事实读取的调用路径使用。

        仍抛参数校验失败而非权限拒绝：调用方据此区分「空间已冻结」与「无权限」。该可
        区分性由调用次序保证，不是自动成立的——次序被改动不会使任何既有用例失败。

        平台级角色一律通过那一行在本特性内无判据来源（平台级角色不属本特性范围），缺失
        方向是更严格：冻结空间的运维介入须先解冻。见 F07 决策 4「五步无判据来源」。
        """
        if not target.org or not target.space:
            return
        if info is None:
            info = self._space_info_if_exists(target)
        if info is None or info.status == SpaceStatus.ACTIVE:
            return
        if info.status in (SpaceStatus.DELETING, SpaceStatus.DELETED):
            if entry == "delete_space":
                return
            raise ValidationError(
                f"space is being deleted: {info.org}/{info.space} status={info.status.value}"
            )
        if action is Action.READ:
            return
        if entry == "archive_space" or (entry == "update_space" and _is_status_only(patch)):
            return
        raise ValidationError(
            f"space is not writable: {info.org}/{info.space} status={info.status.value}"
        )

    def _record_audit(
        self,
        identity: Scope,
        action: str,
        *,
        target_id: str = "",
        target_scope: Scope | None = None,
        decision: str = "allow",
        detail: dict[str, str] | None = None,
    ) -> None:
        payload = dict(detail or {})
        payload.setdefault("decision", decision)
        request_id = get_request_id()
        if request_id:
            payload.setdefault("request_id", request_id)
        self._audit.record(
            AuditEvent(
                id=str(uuid.uuid4()),
                actor=identity,
                action=action,
                target_id=target_id,
                layer="api",
                decision=decision,
                occurred_at=datetime.now(timezone.utc),
                detail=payload,
                target=target_scope or _ROOT,
            )
        )

    def _apply_space_policy_context(
        self,
        target: Scope,
        context: PermissionContext | None,
        *,
        entry: str = "",
        author_principal: str = "",
        carry_author_marks: bool = True,
        space_action: SpaceAction | None = None,
        space_axis: SpaceAxis | None = None,
    ) -> tuple[PermissionContext | None, SpaceFacts | None]:
        """装配判定输入：主体次序、入口名、条目作者标记与空间授权事实。

        判定实现声明需要空间事实时，主体次序取自同一份快照而不再单独读一次策略——
        两处各读一次会使一次调用内的判据基于不同快照。后端异常时事实留空，由判定
        实现按拒绝处理，不沿用过期结果（沿用即为放行方向的失效）。

        连同读到的整份 :class:`SpaceFacts` 一并返回：生命周期状态不进判定投影，而状态
        校验要看它。返回同一份而非另读一次，使一次调用内的判定与状态校验基于同一快照。
        """
        if not target.org or not target.space:
            return context, None
        metadata = dict(context.metadata) if context is not None else {}
        if entry:
            metadata[ATTR_SPACE_ENTRY] = entry
        if space_action is not None:
            # 覆盖入口表的默认动作，用于一个入口按调用形态取不同动作的场景（evolve 的
            # 演进模式、任务入口按发起该作业的模式取值）。
            metadata[ATTR_SPACE_ACTION] = space_action.value
        if space_axis is not None:
            metadata[ATTR_SPACE_AXIS] = space_axis.value
        if author_principal:
            # 是否携带由键是否存在表达，不另设布尔字段——携带时该值恒非空。
            metadata[principal.AUTHOR_PRINCIPAL] = author_principal
        if not carry_author_marks:
            # 条目权限上下文由引擎按条目 metadata 整体构造，作者标记因此自动在内。
            # list 的第二段必须显式剥掉它，理由见 _authorize 的 carry_author_marks。
            metadata.pop(principal.AUTHOR_PRINCIPAL, None)
        space_facts: SpaceAuthorizationFacts | None = None
        facts: SpaceFacts | None = None
        if self._needs_space_facts():
            facts = self._read_space_facts(target)
            if facts is not None:
                metadata[ATTR_PRINCIPAL_PATH] = facts.info.policy.principal_path.value if (
                    facts.info is not None
                ) else PrincipalPath.USER_AGENT.value
                space_facts = _project_space_facts(facts)
        else:
            try:
                policy = self._space.get_policy(target.org, target.space)
            except NotFoundError:
                return self._rebuilt_context(target, context, metadata, None), None
            metadata[ATTR_PRINCIPAL_PATH] = policy.principal_path.value
        return self._rebuilt_context(target, context, metadata, space_facts), facts

    @staticmethod
    def _rebuilt_context(
        target: Scope,
        context: PermissionContext | None,
        metadata: dict[str, str],
        space_facts: SpaceAuthorizationFacts | None,
    ) -> PermissionContext:
        if context is None:
            return PermissionContext(
                resource_type="",
                scope=target,
                metadata=metadata,
                space_facts=space_facts,
            )
        return replace(context, metadata=metadata, space_facts=space_facts)

    def _space_facts_for_guard(self, target: Scope) -> SpaceAuthorizationFacts | None:
        """防护用的空间事实。未装配空间级判定时返回 ``None``，防护随之不执行。

        事实读取失败与未装配不折算成同一返回值：前者拒绝，后者跳过。两者都返回 ``None``
        则后端故障时防护静默失效，而防护失效即提权路径。事实带 TTL 缓存，一次调用内的
        两次读取可以一次命中缓存、一次落到后端，因此该情形不因调用次序而不可达。

        目标缺 org 或 space 维时同样跳过：三处防护中只有授出上界的目标由调用方提供
        （取授予方 scope），而覆盖判定要求两侧 space 维相同，缺 space 维的授权触达不到
        任何空间，防护对它不适用。判断与 ``_apply_space_policy_context`` 取同一条——
        两条读取路径对同一目标得出不同处置，即一条会把空间管理器的入参校验异常抛给
        没有涉及任何空间的调用方。
        """
        if not self._needs_space_facts():
            return None
        if not target.org or not target.space:
            return None
        facts = self._read_space_facts(target)
        if facts is None:
            raise PermissionDeniedError("space facts unavailable: guard cannot be evaluated")
        return _project_space_facts(facts)

    @staticmethod
    def _normalized_member_scope(target: Scope, member: Scope) -> Scope:
        """把成员 scope 补齐到成员表中的形态。

        空间管理器在写入时才补 org 与 space 两维，而防护在写入之前执行——不补齐则
        自提比对的两侧 org 维一空一有值，逐维相同恒不成立，自提禁止整条失效且不报错。
        本归一化须与空间管理器的写入侧保持一致，由专项用例固定。
        """
        return replace(member, org=target.org, space=target.space)

    def _enforce_member_write_ceilings(
        self, identity: Scope, target: Scope, member: SpaceMember
    ) -> None:
        """成员记录写入的两条防护：治理轴授予上界与两轴自提禁止。"""
        facts = self._space_facts_for_guard(target)
        if facts is None:
            return
        ceiling = governance_grade(identity, facts)
        if exceeds_governance_ceiling(member.governance_role, ceiling):
            raise PermissionDeniedError(
                f"governance role {member.governance_role.value!r} exceeds grantor "
                f"ceiling {ceiling.value!r}"
            )
        if raises_own_grade(
            identity,
            self._normalized_member_scope(target, member.scope),
            facts,
            member.content_role,
            member.governance_role,
        ):
            raise PermissionDeniedError("a member record must not raise the caller's own grade")

    def _enforce_member_removal_ceiling(
        self, identity: Scope, target: Scope, member: Scope
    ) -> None:
        """移除成员的授予上界：改前值同受约束，因此不能移除档位高于自己的成员。"""
        facts = self._space_facts_for_guard(target)
        if facts is None:
            return
        ceiling = governance_grade(identity, facts)
        normalized = self._normalized_member_scope(target, member)
        for existing in facts.members:
            if existing.scope != normalized:
                continue
            if exceeds_governance_ceiling(existing.governance_role, ceiling):
                raise PermissionDeniedError(
                    f"member governance role {existing.governance_role.value!r} exceeds "
                    f"remover ceiling {ceiling.value!r}"
                )
            return

    def _enforce_grant_ceiling(self, identity: Scope, grant: Grant) -> None:
        """显式授权的授出上界：授出动作须为授予方内容轴有效集合的子集。

        「本人所写」附加集合不计入基数：它以「这一条是本人所写」为条件、逐条成立，
        授出去即失去条件。
        """
        facts = self._space_facts_for_guard(grant.grantor)
        if facts is None:
            return
        ceiling = content_grade(identity, facts)
        requested = {
            action
            for action in (_GRANT_ACTION_BACK.get(item) for item in grant.actions)
            if action is not None
        }
        excess = requested - ceiling
        if excess:
            raise PermissionDeniedError(
                f"granted actions exceed the grantor content ceiling: "
                f"{sorted(action.value for action in excess)}"
            )

    def _invalidate_space_facts(self, org: str, space: str) -> None:
        """治理写入提交后使空间事实缓存失效。

        降权即时生效依赖它：不下发失效，被移除的成员在缓存 TTL 内仍按旧快照通过判定，
        而该窗口内的放行不产生任何错误信号——症状是「移除了但还能访问几秒」，既无异常
        也无审计差异，只能靠专项用例发现。

        失效在写入提交之后下发，不在之前：提前下发则写入失败时缓存已被清空，下一次读取
        重新装填的仍是旧事实，等于白清一次。
        """
        if self._membership is None:
            return
        self._membership.invalidate(org, space)

    def _needs_space_facts(self) -> bool:
        return self._membership is not None and self._perm.requires_space_facts()

    def _read_space_facts(self, target: Scope) -> SpaceFacts | None:
        """取一次空间授权事实；后端不可用时返回 ``None``，由判定实现按拒绝处理。"""
        try:
            return self._membership.facts(target.org, target.space)
        except (BackendError, NotFoundError):
            return None

    def _authorize(
        self,
        identity: Scope,
        target: Scope,
        action: Action,
        audit_action: str,
        target_id: str = "",
        *,
        context: PermissionContext | None = None,
        check_permission: bool = True,
        require_space: bool = True,
        author_principal: str = "",
        carry_author_marks: bool = True,
        space_action: SpaceAction | None = None,
        space_axis: SpaceAxis | None = None,
        space_patch: SpacePatch | None = None,
    ) -> dict[str, str]:
        return self._authorize_with_context(
            identity,
            target,
            action,
            audit_action,
            target_id,
            context=context,
            check_permission=check_permission,
            require_space=require_space,
            author_principal=author_principal,
            carry_author_marks=carry_author_marks,
            space_action=space_action,
            space_axis=space_axis,
            space_patch=space_patch,
        )[0]

    def _authorize_with_context(
        self,
        identity: Scope,
        target: Scope,
        action: Action,
        audit_action: str,
        target_id: str = "",
        *,
        context: PermissionContext | None = None,
        check_permission: bool = True,
        require_space: bool = True,
        author_principal: str = "",
        carry_author_marks: bool = True,
        space_action: SpaceAction | None = None,
        space_axis: SpaceAxis | None = None,
        space_patch: SpacePatch | None = None,
    ) -> tuple[dict[str, str], PermissionContext | None]:
        """鉴权并连同装配好的权限上下文一并返回。

        检索谓词第一族要用鉴权时已读的那一份空间事实。另起一次读取会拿到另一个快照
        （事实带 TTL 缓存，跨越 TTL 边界即两份），使一次调用内的入口鉴权与条目过滤基于
        不同事实——症状是极低频的「刚加的成员搜不到自己刚写的条目」，不可复现。
        """
        if self._needs_space_facts() and _is_space_level_entry(audit_action):
            # 空间级入口的形态校验：主体维全空即拒绝。两处限定各有理由——
            # 只在空间级判定装配时执行，是因为现有装配把空身份当运维通道，无条件加会
            # 收紧既有行为；只对空间级入口执行，是因为组织级入口由角色闸门裁决，本特性内
            # 无组织级角色、回落父类判据，空身份仍是该通道的唯一形态（如开通服务建空间）。
            principal.require_principal(identity)
        effective_context, space_facts = self._apply_space_policy_context(
            target,
            context,
            entry=audit_action,
            author_principal=author_principal,
            carry_author_marks=carry_author_marks,
            space_action=space_action,
            space_axis=space_axis,
        )
        if _missing_required_space(self._policy, target, require_space):
            self._record_audit(
                identity,
                audit_action,
                target_id=target_id,
                target_scope=target,
                decision="deny",
                detail={
                    "permission_check": "enabled",
                    "permission_reason": "scope.space is required",
                    **_context_detail(effective_context),
                },
            )
            raise ValidationError("scope.space is required")
        if not check_permission:
            return (
                {
                    "permission_check": "disabled",
                    "permission_reason": "permission check disabled",
                    **_context_detail(effective_context),
                },
                effective_context,
            )
        outcome = self._perm.decide(identity, target, action, context=effective_context)
        if not outcome.allowed:
            self._record_audit(
                identity,
                audit_action,
                target_id=target_id,
                target_scope=target,
                decision="deny",
                detail={
                    "permission_check": "enabled",
                    "permission_reason": f"permission denied for action={action.value}",
                    "permission_rule": outcome.rule,
                    **_context_detail(effective_context),
                },
            )
            raise PermissionDeniedError(action.value)
        if self._needs_space_facts() and _is_space_level_entry(audit_action):
            # 后置校验：排在授权通过之后。只在空间级判定装配时执行，与形态校验同一理由——
            # 改造前只有两处写路径做状态校验，无条件扩到全部入口会改变既有装配的行为。
            self._ensure_space_state_allows(
                target,
                action,
                audit_action,
                info=space_facts.info if space_facts is not None else None,
                patch=space_patch,
            )
        return (
            {
                "permission_check": "enabled",
                "permission_reason": "permission check passed",
                "permission_rule": outcome.rule,
                # 通过的轴同时是空间策略裁剪的判据，落审计使「谁凭哪条轴读到了策略」可追溯。
                "permission_axis": outcome.axis.value if outcome.axis is not None else "",
                **_context_detail(effective_context),
            },
            effective_context,
        )

    def _log(
        self,
        identity: Scope,
        action: str,
        target_id: str = "",
        *,
        target_scope: Scope | None = None,
        decision: str = "allow",
        detail: dict[str, str] | None = None,
    ) -> None:
        self._record_audit(
            identity,
            action,
            target_id=target_id,
            target_scope=target_scope,
            decision=decision,
            detail=detail,
        )

    def check_write(
        self,
        scope: Scope,
        security: RequestSecurityContext,
        *,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
    ) -> None:
        """Pre-flight WRITE 鉴权（不落盘）。镜像 add 的鉴权路径，但不调 engine.write；
        供长耗时摄入任务入队前拒绝无权限请求（P1-2 防 DoS）。
        """
        identity = security.auth.actor
        _reject_non_scalar_metadata(system_metadata, field_name="system_metadata")
        _reject_non_scalar_metadata(user_metadata, field_name="user_metadata")
        permission_context = _write_permission_context(scope, tags, system_metadata)
        self._authorize(
            identity,
            scope,
            Action.WRITE,
            "check_write",
            context=permission_context,
        )
        self._ensure_space_writable(scope)
