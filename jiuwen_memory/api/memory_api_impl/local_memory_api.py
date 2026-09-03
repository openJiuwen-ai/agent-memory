# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""LocalMemoryAPI facade: PEP + typed Control application ports.

Implementation helpers live in ``local_support.py`` and ``*_ops.py`` mixins.
Public class and method signatures are unchanged.
"""

from __future__ import annotations

from jiuwen_memory.api.memory_api import MemoryAPI
from jiuwen_memory.common.audit import AuditLogger
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.audit_integrity.base import (
    AuditIntegrityProvider,
    AuditVerificationLimits,
)
from jiuwen_memory.common.security.protection.workload_guard import WorkloadGuard
from jiuwen_memory.construction.router import EMPTY_ROUTE_TABLE, Router, RouteTable
from jiuwen_memory.control import collective
from jiuwen_memory.control.application import (
    GovernanceService,
    MemoryCommandService,
    MemoryQueryService,
    SpaceLifecycleService,
)
from jiuwen_memory.control.engine import MemoryEngine
from jiuwen_memory.control.governance import Governor
from jiuwen_memory.control.ingest_job import IngestJobController
from jiuwen_memory.control.membership import MembershipResolver
from jiuwen_memory.control.permission import PermissionManager
from jiuwen_memory.control.policy import PolicyManager
from jiuwen_memory.control.scheduler import Scheduler
from jiuwen_memory.control.space import SpaceManager

from .admin_ops import AdminOpsMixin
from .local_support import (
    _first_family_predicate,
    _policy_int,
    _resolve_space_owner,
)
from .pep_ops import PepOpsMixin
from .query_ops import QueryOpsMixin
from .space_ops import SpaceOpsMixin
from .write_ops import WriteOpsMixin

__all__ = ["LocalMemoryAPI", "_first_family_predicate", "_resolve_space_owner"]


class LocalMemoryAPI(
    WriteOpsMixin,
    QueryOpsMixin,
    AdminOpsMixin,
    SpaceOpsMixin,
    PepOpsMixin,
    MemoryAPI,
):
    """单进程装配下的统一记忆接口实现（鉴权 + 审计 + 委派）。"""

    def __init__(
        self,
        engine: MemoryEngine,
        permission: PermissionManager,
        scheduler: Scheduler,
        policy: PolicyManager,
        governor: Governor,
        audit_logger: AuditLogger,
        space: SpaceManager,
        ingest_jobs: IngestJobController,
        audit_integrity_provider: AuditIntegrityProvider | None = None,
        audit_verify_guard: WorkloadGuard | None = None,
        audit_verify_limits: AuditVerificationLimits | None = None,
        membership: MembershipResolver | None = None,
        router: Router | None = None,
        commands: MemoryCommandService | None = None,
        queries: MemoryQueryService | None = None,
        space_lifecycle: SpaceLifecycleService | None = None,
        governance: GovernanceService | None = None,
    ) -> None:
        if audit_integrity_provider is not None and audit_verify_guard is None:
            raise ValidationError(
                "audit_integrity_provider requires a dedicated audit_verify_guard"
            )
        if audit_verify_limits is not None and not isinstance(
            audit_verify_limits, AuditVerificationLimits
        ):
            raise ValidationError("audit_verify_limits must be AuditVerificationLimits")
        self._engine = engine
        self._perm = permission
        self._scheduler = scheduler
        self._policy = policy
        self._governor = governor
        self._audit = audit_logger
        self._space = space
        self._ingest_jobs = ingest_jobs
        # 审计完整性 provider：未装配（None）时 verify_audit 返回 unsupported。装配后
        # 由本层 verify_audit 经 provider.verify 流式校验证明链。provider 持有的
        # ChainedAuditStore 与 self._audit 须是同一具名实例（装配侧保证）。
        self._audit_integrity = audit_integrity_provider
        # verify_audit 的全量验证是重操作，占专用 WorkloadGuard 的一个并发槽。
        # 未装配 provider 时 guard 可为空（verify 直接返回 unsupported）；provider 与
        # guard 必须成对注入，避免完整性验证无预算运行或与认证路径争抢同一预算。
        self._audit_verify_guard = audit_verify_guard
        # 可信服务端装配值；不从 verify_audit payload 或 provider 返回读取。
        self._audit_verify_limits = audit_verify_limits or AuditVerificationLimits()
        # 空间授权事实的读取算子。取可选参数是为了不改变既有装配的构造签名：不做
        # 空间级判定的部署无须提供它，此时判定实现的 requires_space_facts 也为假。
        self._membership = membership
        # 归属判定算子。未装配时判定表为空，写入侧 scope 必填、判定路径不可达，
        # 全链路行为与未启用该特性一致——这是可灰度上线的前提。
        self._router = router
        self._commands = commands if commands is not None else MemoryCommandService(engine)
        self._queries = queries if queries is not None else MemoryQueryService(engine)
        self._space_lifecycle = (
            space_lifecycle
            if space_lifecycle is not None
            else SpaceLifecycleService(engine, space)
        )
        self._governance = governance if governance is not None else GovernanceService(governor)

    @property
    def space_governance_enabled(self) -> bool:
        """本次装配是否启用空间治理，即判定实现是否读空间事实。

        供接入方与示例判断部署形态：启用后写入未注册空间由放行改为拒绝，调用方须先
        开通空间；未启用时 ``scope`` 的空间维可留空，行为与改造前一致。
        """
        return self._needs_space_facts()

    @property
    def route_table(self) -> RouteTable:
        """判定表的对外只读视图，供仓库内运维脚本（存量回填）取判定标签键集合。

        与内部使用的 :attr:`_route_table` 同源：运维脚本另读一次配置解析出的产物可能
        与运行时不一致，届时回填写入的标签键与判定实际使用的键集合会分叉。
        """
        return self._route_table

    @property
    def _route_table(self) -> RouteTable:
        """本次装配的判定表；未装配判定算子时为空表。

        取自判定算子实例而不另读一次配置：两条解析路径对同一份配置得出不同产物时，
        写入边界拒绝的键集合与判定实际写入的键集合会不一致，表现为判定自己写的标签
        在下一次写入时被自己的边界校验拒绝。
        """
        return self._router.table if self._router is not None else EMPTY_ROUTE_TABLE

    def _routing_enabled(self) -> bool:
        return not self._route_table.is_empty()

    def _space_fanout_limit(self) -> int:
        """一次调用参与的空间数上限，写入侧与检索侧同值（策略 ``space.fanout_limit``）。

        这是功能天花板而非性能参数：主体同时参与的空间超过该值时，超出部分写入不进候选、
        检索也取不到。够用与否取决于接入方的协作规模假设，因此可配置而不写死在内核里。

        两侧同值是有意的：写入侧按前 N 个候选落点，检索侧按前 N 个候选取数，取值分叉即
        出现「写得进去但检索不到」。
        """
        return _policy_int(
            self._policy, "space.fanout_limit", default=collective.SPACE_FANOUT_LIMIT
        )

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
        | ``DELETING`` / ``DELETED`` | 全部动作拒绝，不适用任何豁免 |
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

    def _purge_space_memories(self, scope: Scope) -> list[str]:
        return asyncio.run(self._engine.purge_space(scope.org, scope.space))

    # -- 鉴权 + 审计公共点 --------------------------------------------------- #

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

    # -- 改写防护与两处上界（F07「改写防护与三处上界」） --------------------- #
    #
    # 判定依据有三类（成员角色、空间策略、显式授权记录），三者的写入路径都须防护：
    # 角色成为鉴权来源之后，缺少防护的写入路径即提权路径。防护与判定链同期交付。
    #
    # 防护边界是本类。边界之外有三项须随发布说明声明：进程内直接持有空间管理器可绕过
    # 全部防护、直调不触发事实缓存失效、装配为不做空间级判定的实现时空间语义整体不生效。

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

    # -- 数据面 ------------------------------------------------------------- #

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

    def add(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        return asyncio.run(
            self.add_async(
                content,
                scope,
                source,
                security=security,
                assets=assets,
                tags=tags,
                system_metadata=system_metadata,
                user_metadata=user_metadata,
                occurred_at=occurred_at,
            )
        )

    async def add_async(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        identity = security.auth.actor
        _reject_invalid_content(content)
        # 三项校验都在落点解析之前：解析会往 system_metadata 塞判定产物与瞬态的
        # route_ctx（非标量），先校验才是校调用方给的那份。
        # 坐标先取出：它是嵌套字典，留在参数袋里会被下面的标量校验拒绝。
        coords, system_metadata = _take_coords(
            system_metadata, enabled=self._routing_enabled()
        )
        _reject_kernel_system_metadata(system_metadata)
        _reject_route_tag_keys(system_metadata, self._route_table.tag_keys)
        _reject_non_scalar_metadata(system_metadata, field_name="system_metadata")
        _reject_non_scalar_metadata(user_metadata, field_name="user_metadata")
        reject_kernel_coords(coords)
        target, system_metadata = self._write_target(
            scope, identity, coords, content, system_metadata
        )
        permission_context = _write_permission_context(target, tags, system_metadata)
        auth = self._authorize(
            identity,
            target,
            Action.WRITE,
            "add",
            context=permission_context,
        )
        self._ensure_space_writable(target)
        units = await self._engine.write(
            content,
            target,
            source,
            assets=assets,
            tags=tags,
            system_metadata=_with_author_marks(system_metadata, identity),
            user_metadata=user_metadata,
            occurred_at=occurred_at,
        )
        self._log(identity, "add", target_scope=target, detail=auth)
        return units

    def _routes_by_decision(
        self,
        scope: Scope | None,
        coords: Mapping[str, str] | None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """本次写入是否交给归属判定：由参数袋里有没有 ``coords`` 键决定。

        判据取键的有无，不取 ``scope`` 的取值形态。「判定表非空」即
        :meth:`_routing_enabled`，取值来自配置的 ``router`` 命名空间：

        | 判定表非空 | ``coords`` 键 | ``scope.space`` | 处置 |
        |---|---|---|---|
        | 否 | 任意 | 任意 | 直写 |
        | 是 | 有 | 任意 | 走判定，``scope.space`` 不参与落点 |
        | 是 | 无 | 非空 | 直写 |
        | 是 | 无 | 空 | 拒绝：既未指定落点，也未请求判定 |

        **``middle=true`` 不判定**（F07「归属判定的生效范围」），按无 ``coords`` 键的三行
        处置。该路径把原文按 ``tier=WORKING`` 直接建索引（见
        :meth:`~control.engine_impl.in_memory_engine.InMemoryEngine._write_middle_path`），
        与 ``infer`` / ``procedural`` 的载体不同——后两者的原文进 ``message_store``、不参与
        检索。走判定分流则它落 fallback 且一个收窄维标签键都不落，随后在任何带 ``coords``
        的检索里被第二族谓词 ``IN ["", value]`` 静默排除，正是不变量 8 要避开的形态。

        **判据是调用方的一个动作，不是某个字段的缺省状态。** ``space`` 为空是调用方什么都
        没表达时的取值，拿它触发另一条处理路径等于由内核替调用方解读缺省值。

        **``space`` 非空不作反向判据。** 上游网关按自己的租户或应用标识填 ``space`` 是常见
        形态，那个取值不是本系统的空间标识。以它否决判定请求，这类接入方将无路可走——直写要求
        该空间已在本系统登记，未登记即判权拒绝，且内核不为调用方给的空间名自动创建（见
        :meth:`_ensure_fallback_space`）。判定请求因而优先；交出落点决定权是 ``coords`` 键
        的既定语义，真实落点又由返回的记忆单元携带。

        **键的有无与值的内容分开。** ``coords`` 为 ``{}`` 是合法的判定请求，表示「请判定，
        但本次没有业务坐标」——内核坐标由 ``kernel_coords`` 从身份填入。取值判空则这层意图
        只能退回缺省状态触发，正是本判据要避开的形态。

        **末行的拒绝替换的是原本的判权拒绝**：启用判定的部署里空 ``space`` 拿不到空间事实，
        报出 ``permission denied``，与真正的越权不可区分。未装配判定算子的部署不受影响——
        ``space`` 为空是那里的既有合法落点（``InMemoryEngine`` 要求 ``space`` 为空串），
        该拒绝不成立。
        """
        if not self._routing_enabled():
            return False
        if scope is not None and not isinstance(scope, Scope):
            # 类型校验须先于 scope.space，否则非 Scope 入参在这里得到的是 AttributeError
            # 而不是 ValidationError；直写路径的同名校验在本判据之后，兜不到这条。
            raise ValidationError("scope must be Scope")
        if coords is not None and not _truthy_metadata(metadata, "middle"):
            return True
        if scope is None or not scope.space:
            raise ValidationError(
                "写入落点未声明：给 scope.space 指定落点，"
                f"或给 system_metadata[{COORDS_KEY!r}] 交由归属判定"
            )
        if coords is not None:
            # 坐标本次不生效，记 WARNING 而不静默丢弃：「以为坐标生效了、其实没有」从
            # 调用侧看不出差别。排在落点校验之后——落点未声明时抛出的异常已是终局信号，
            # 再记一条告警只是噪声。
            logger.warning(
                "write: middle=true 不参与归属判定，本次 %r 不生效，落点取 scope.space",
                COORDS_KEY,
            )
        return False

    def _write_target(
        self,
        scope: Scope,
        identity: Scope,
        coords: dict[str, str] | None,
        content: str,
        metadata: dict[str, Any] | None,
    ) -> tuple[Scope, dict[str, Any] | None]:
        """定妥本次写入的落点与要补写的判定产物。

        落点有两个来源，不叠加：请求判定即以判定结果为落点，``scope.space`` 不再参与。
        分流判据见 :meth:`_routes_by_decision`；走到本方法体内的只有三种形态：

        | 调用形态 | 落点 | 判定 |
        |---|---|---|
        | 给 ``space`` | 就是它（启用判定时归一为空间级两维） | 否 |
        | 给 ``coords`` + ``infer``/``procedural`` 为真 | fallback 空间作载体 | 在构建层 |
        | 给 ``coords`` + 其余情形 | 判定算子在候选集内选 | 在本层，整条内容作一条候选送判 |

        ``middle=true`` 不在表内：它在分流判据处即被排除，走第一行（见
        :meth:`_routes_by_decision`）。

        原文与消息维护落 fallback 空间，判定只改派生单元的 scope，引擎写入签名不变。
        """
        if not self._routes_by_decision(scope, coords, metadata):
            return self._explicit_scope_target(scope, identity, metadata)
        _reject_foreign_routed_scope(scope, identity)
        outcome = self._routed_targets([(0, content, metadata)], identity, coords)[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _explicit_scope_target(
        self, scope: Scope | None, identity: Scope, metadata: dict[str, Any] | None
    ) -> tuple[Scope, dict[str, Any] | None]:
        """调用方传了 ``scope`` 的那条路径：不判定，只做归一与两项补齐。"""
        if scope is None:
            # 只有批量入口到得了这里：两级 scope 都缺省，批级参数袋又没请求判定。
            # 措辞与改造前一致——该形态在改造前后都是同一种调用错误。
            raise ValidationError("batch item scope is required")
        if not isinstance(scope, Scope):
            raise ValidationError("scope must be Scope")
        if not self._routing_enabled():
            return scope, metadata
        # 主体维校验与落盘 scope 归一同时生效，两者是同一件事的两半：归一把主体维从
        # 落盘键上去掉，校验保证去掉的那部分与调用方身份一致而不是被静默丢弃。未启用
        # 判定的部署两者都不做——落盘 scope 语义未变，跨主体写入照旧由判定链拒绝，
        # 拒绝类型也照旧是 PermissionDeniedError。
        _reject_foreign_write_scope(scope, identity)
        # 判定标签键在这条路径上同样补齐：不变量的定义域是落盘条目而非判定产物。
        # 不补则同一空间内两条写入路径产出的条目在带 coords 的检索里表现不同——
        # 判定路径写的能召回，这条路径写的被静默漏掉，且调用方不会收到任何提示。
        # 取值一律空串：本路径不经判定，「这条属于哪个项目」无从得知，而空串的语义
        # 是「不特定于任何坐标，因此对任何坐标都可见」，与判为否的条目同一待遇。
        return _space_level_scope(scope), fill_missing_tag_keys(
            metadata, self._route_table.tag_keys
        )

    def _routed_targets(
        self,
        entries: Sequence[tuple[Any, str, dict[str, Any] | None]],
        identity: Scope,
        coords: dict[str, str] | None,
    ) -> dict[Any, tuple[Scope, dict[str, Any] | None] | Exception]:
        """交由归属判定的写入：整批一次判定，逐项产出落点与要补写的判定产物。

        入参每项是 ``(key, content, metadata)``，返回按 key 索引；判定失败的项返回异常对象
        而不抛出，由调用处按各自的 ``continue_on_error`` 语义处置——批量入口里一项失败不该
        使整批中止。

        **判定上下文每批只算一次。** ``coords`` 与 ``identity`` 批内恒定，候选空间集合与
        逐空间判权因而是同一份；逐项各算一次即把判权次数乘以批大小。判定本身也整批一次送判，
        理由见 :func:`~control.collective.routing.route_many`。
        """
        if not entries:
            return {}
        if not self._routing_enabled():
            # 防御性分支：两个调用点都经 _routes_by_decision 前置，未装配判定表时不会走到
            # 这里。留着是因为本方法的正确性不该依赖调用点记得先判——真到了这里，说明
            # 分流判据与本方法的前提脱了钩，报错比按空判定表继续算下去可诊断。
            error = ValidationError(
                f"system_metadata[{COORDS_KEY!r}] is not a routing request here: "
                "no routing table is configured, the decision path is unreachable"
            )
            return {key: error for key, _content, _metadata in entries}
        try:
            ctx = self._route_context(identity, coords)
        except Exception as exc:  # noqa: BLE001 —— 上下文构造失败逐项回传，不中止整批
            return {key: exc for key, _content, _metadata in entries}

        resolved: dict[Any, tuple[Scope, dict[str, Any] | None] | Exception] = {}
        pending: list[tuple[Any, str, dict[str, Any]]] = []
        for key, content, metadata in entries:
            merged = dict(metadata or {})
            if _truthy_metadata(merged, "infer") or _truthy_metadata(merged, "procedural"):
                # 派生路径：判定在构建层逐条进行，本层只把上下文经瞬态键传下去。载体（原文与
                # 消息维护）落 fallback 空间——它是本次候选集内最窄的那个。
                #
                # **为什么走 system_metadata 而不是 extensions。** F04「运行时对象不塞进
                # metadata」要求这类调用级依赖统一经 ``Context.extensions`` 透传，但
                # ``add`` / ``batch_add`` 的签名里没有 ``Context`` 参数——``extensions``
                # 是检索侧的通道，写入路径上不存在，到不了引擎内部的抽取链路。本层与构建层
                # 之间唯一贯通的容器就是 ``system_metadata``。
                #
                # 因此它按瞬态键处置，三条约束把「不进持久元数据语义」这层意思补回来：
                # 编解码器序列化时剥除、不落盘（``TRANSIENT_SYSTEM_METADATA_KEYS``）；
                # 不进权限上下文（见 ``_write_permission_context``）；``MetadataValueType``
                # 不因它放宽，只在写入边界的标量校验里对该键单独放行。
                merged[ROUTE_CTX_KEY] = ctx
                resolved[key] = (ctx.fallback, merged)
                continue
            pending.append((key, content, merged))
        if pending:
            decisions = collective.route_many(
                self._router, [content for _key, content, _meta in pending], ctx
            )
            for (key, _content, merged), decision in zip(pending, decisions):
                merged.update(collective.decision_metadata(decision))
                resolved[key] = (decision.scope, merged)
            self._log_routing_degradation(identity, ctx, decisions)
        return resolved

    def _log_routing_degradation(
        self, identity: Scope, ctx: RouteContext, decisions: Sequence[RouteDecision]
    ) -> None:
        """把本批里没按判定原样落点的条数与逐原因计数记进审计。

        判定降级不阻断写入，因此在调用方看来它与「判定就是这么判的」完全一样：落点是
        fallback、内容照常落盘、不抛异常。``RouteDecision.reason`` 已经把原因区分开，但
        它止于本层——``decision_metadata`` 只回写判定标签与类别记录键，不带 reason，落盘
        条目上因此看不出本条是判出来的还是回落来的。

        **本路径落审计而非日志，判据是记录的消费者**（F07「降级记录按消费者分通道」）：
        结论直写的落点是调用方动作的直接结果，须可按 actor / target 检索并逐次回放。派生
        单元的判定在构建层，消费者是运维告警，落 ``WARNING`` 日志——见
        ``OrchestratingEvolver._route``。两条路径共用同一套 ``reason`` 词汇。

        **逐原因计数不可省。** 四类降级的处置完全不同：未装配是部署形态、判定器故障要查
        插件、条数不符是实现违约、落点越界是判定试图扩权。只报总数时说得出「有降级」，
        说不出该找谁。

        按原因聚合记一条，不逐条记：``coords`` 与候选集批内恒定，同一批的降级原因通常同源，
        逐条记只是把同一句话重复 N 遍。原样落点的条目不记——那是正常路径。
        """
        counted = degraded_reasons(decisions)
        if not counted:
            return
        self._log(
            identity,
            "add",
            ctx.fallback.space,
            target_scope=ctx.fallback,
            detail={
                "entry": "routing_degraded",
                "degraded": str(sum(counted.values())),
                "total": str(len(decisions)),
                "reasons": "; ".join(
                    f"{reason} x{count}" for reason, count in counted.most_common()
                ),
            },
        )

    def _batch_write_targets(
        self,
        entries: Sequence[tuple[int, BatchWriteItem]],
        identity: Scope,
        coords: dict[str, str] | None,
    ) -> dict[int, tuple[Scope, dict[str, Any] | None] | Exception]:
        """批量入口的落点解析：显式 ``scope`` 的项逐项处理，其余整批一次判定。"""
        resolved: dict[int, tuple[Scope, dict[str, Any] | None] | Exception] = {}
        routed: list[tuple[int, str, dict[str, Any] | None]] = []
        for index, item in entries:
            # 分流判据本身会拒绝两种落点声明形态（见 _routes_by_decision），拒绝逐项回传
            # 而不中止整批，与其余两类失败同一处置。coords 是批级的，逐项判据只差 scope。
            try:
                routes = self._routes_by_decision(item.scope, coords, item.system_metadata)
                if routes:
                    _reject_foreign_routed_scope(item.scope or Scope(), identity)
                else:
                    resolved[index] = self._explicit_scope_target(
                        item.scope, identity, item.system_metadata
                    )
            except Exception as exc:  # noqa: BLE001 —— 逐项回传，与判定路径同一处置
                resolved[index] = exc
                continue
            if routes:
                routed.append((index, item.content, item.system_metadata))
        resolved.update(self._routed_targets(routed, identity, coords))
        return resolved

    @staticmethod
    def _batch_error_item(item: object) -> BatchWriteItem:
        if isinstance(item, BatchWriteItem):
            return item
        return BatchWriteItem(content="")

    @staticmethod
    def _merge_batch_tags(
        defaults: list[str] | None, item_tags: list[str] | None
    ) -> list[str] | None:
        if defaults is None and item_tags is None:
            return None
        merged: list[str] = []
        for tag in [*(defaults or []), *(item_tags or [])]:
            if tag not in merged:
                merged.append(tag)
        return merged

    @staticmethod
    def _batch_outcome(
        index: int, item: object, error: Exception, *, error_type: str | None = None
    ) -> BatchWriteOutcome:
        return BatchWriteOutcome(
            index=index,
            item=LocalMemoryAPI._batch_error_item(item),
            error=str(error),
            error_type=error_type or type(error).__name__,
        )

    def _normalize_batch_item(
        self,
        item: object,
        *,
        scope: Scope | None,
        source: Modality,
        tags: list[str] | None,
        system_metadata: dict[str, MetadataValueType] | None,
        user_metadata: dict[str, MetadataValueType] | None,
        occurred_at: datetime | None,
        stream_id: str,
    ) -> BatchWriteItem:
        """归一一项批量写入；逐项 ``scope`` 为 ``None`` 即沿用批级取值。

        两级都不给时归一结果为 ``None``，本处放行：判定请求是批级的，批级参数袋带 ``coords``
        时落点由判定给出，此时要求某一级给出 ``scope`` 是多余的。两级都不给又没请求判定的
        情形在调用处按 :meth:`_routes_by_decision` 分流时拒绝。
        """
        if not isinstance(item, BatchWriteItem):
            raise ValidationError("batch item must be BatchWriteItem")
        _reject_invalid_content(item.content)
        target_scope = item.scope if item.scope is not None else scope
        if target_scope is not None and not isinstance(target_scope, Scope):
            raise ValidationError("batch item scope must be Scope")
        item_source = item.source if item.source is not None else source
        if not isinstance(item_source, Modality):
            raise ValidationError("batch item source must be Modality")
        if item.assets is not None and (
            not isinstance(item.assets, list)
            or any(not isinstance(asset, str) for asset in item.assets)
        ):
            raise ValidationError("batch item assets must be list[str]")
        for values, name in ((tags, "tags"), (item.tags, "item tags")):
            if values is not None and (
                not isinstance(values, list) or any(not isinstance(value, str) for value in values)
            ):
                raise ValidationError(f"batch {name} must be list[str]")
        if item.system_metadata is not None and not isinstance(item.system_metadata, dict):
            raise ValidationError("batch item system_metadata must be dict")
        if item.user_metadata is not None and not isinstance(item.user_metadata, dict):
            raise ValidationError("batch item user_metadata must be dict")
        if item.occurred_at is not None and not isinstance(item.occurred_at, datetime):
            raise ValidationError("batch item occurred_at must be datetime")
        if item.system_metadata and COORDS_KEY in item.system_metadata:
            # 静默忽略的失效方向是「以为逐项坐标生效了、其实没有」，从调用侧看不出来。
            raise ValidationError(
                f"batch item 不得携带 {COORDS_KEY}：判定上下文每批只算一次，坐标取自批级参数袋"
            )
        merged_system_metadata = {
            **(system_metadata or {}),
            **(item.system_metadata or {}),
        }
        merged_user_metadata = {**(user_metadata or {}), **(item.user_metadata or {})}
        # 逐项校验而不是只校批级默认值：逐项的 system_metadata 同样是调用方入参。
        _reject_kernel_system_metadata(merged_system_metadata)
        _reject_route_tag_keys(merged_system_metadata, self._route_table.tag_keys)
        _reject_non_scalar_metadata(merged_system_metadata, field_name="system_metadata")
        _reject_non_scalar_metadata(merged_user_metadata, field_name="user_metadata")
        if item.stream_id and not isinstance(item.stream_id, str):
            raise ValidationError("batch item stream_id must be str")
        if item.sequence is not None and not isinstance(item.sequence, int):
            raise ValidationError("batch item sequence must be int")
        if not isinstance(item.idempotency_key, str):
            raise ValidationError("batch item idempotency_key must be str")
        return BatchWriteItem(
            content=item.content,
            scope=target_scope,
            source=item_source,
            assets=list(item.assets) if item.assets is not None else None,
            tags=self._merge_batch_tags(tags, item.tags),
            system_metadata=merged_system_metadata or None,
            user_metadata=merged_user_metadata or None,
            occurred_at=item.occurred_at if item.occurred_at is not None else occurred_at,
            stream_id=item.stream_id or stream_id,
            sequence=item.sequence,
            idempotency_key=item.idempotency_key,
        )

    # -- 数据面 ------------------------------------------------------------- #

    def batch_add(
        self,
        items: list[BatchWriteItem],
        scope: Scope | None = None,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
        stream_id: str = "",
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        return asyncio.run(
            self.batch_add_async(
                items,
                scope,
                source,
                security=security,
                tags=tags,
                system_metadata=system_metadata,
                user_metadata=user_metadata,
                occurred_at=occurred_at,
                stream_id=stream_id,
                continue_on_error=continue_on_error,
            )
        )

    async def batch_add_async(
        self,
        items: list[BatchWriteItem],
        scope: Scope | None = None,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
        stream_id: str = "",
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        identity = security.auth.actor
        if not isinstance(items, list) or not items:
            raise ValidationError("batch items must be a non-empty list")
        if scope is not None and not isinstance(scope, Scope):
            raise ValidationError("batch scope must be Scope")
        if not isinstance(source, Modality):
            raise ValidationError("batch source must be Modality")
        if tags is not None and (
            not isinstance(tags, list) or any(not isinstance(value, str) for value in tags)
        ):
            raise ValidationError("batch tags must be list[str]")
        if system_metadata is not None and not isinstance(system_metadata, dict):
            raise ValidationError("batch system_metadata must be dict")
        if user_metadata is not None and not isinstance(user_metadata, dict):
            raise ValidationError("batch user_metadata must be dict")
        if not isinstance(stream_id, str):
            raise ValidationError("batch stream_id must be str")
        if occurred_at is not None and not isinstance(occurred_at, datetime):
            raise ValidationError("batch occurred_at must be datetime")
        # 坐标取自批级参数袋：判定上下文每批只算一次，逐项无处安放（逐项携带即拒绝，
        # 见 _normalize_batch_item）。同样须先于标量校验取出。
        coords, system_metadata = _take_coords(
            system_metadata, enabled=self._routing_enabled()
        )
        _reject_kernel_system_metadata(system_metadata)
        _reject_route_tag_keys(system_metadata, self._route_table.tag_keys)
        _reject_non_scalar_metadata(system_metadata, field_name="system_metadata")
        _reject_non_scalar_metadata(user_metadata, field_name="user_metadata")
        reject_kernel_coords(coords)

        outcomes: dict[int, BatchWriteOutcome] = {}
        ready: list[tuple[int, BatchWriteItem]] = []
        seen_sequences: set[tuple[str, str, str, str, str, str, int]] = set()
        stopped_index: int | None = None

        def _record_failure(index: int, raw_item: object, exc: Exception) -> None:
            if not isinstance(exc, (ValidationError, PermissionDeniedError, PolicyError)):
                raise exc
            outcomes[index] = self._batch_outcome(index, raw_item, exc)
            error_scope = (
                raw_item.scope
                if isinstance(raw_item, BatchWriteItem) and isinstance(raw_item.scope, Scope)
                else scope
            )
            self._log(
                identity,
                "add",
                target_scope=error_scope,
                decision="error",
                detail={"error": str(exc), "error_type": type(exc).__name__},
            )

        # 第 1 遍：规范化。落点解析不在这一遍——space 为空的项要整批一次送判，逐项各判
        # 一次会使 Router「每批一次模型调用」的契约在批量入口整段失效（N 条即 N 次串行
        # 调用），候选空间集合的逐空间判权也跟着乘以批大小。
        normalized: list[tuple[int, BatchWriteItem]] = []
        for index, raw_item in enumerate(items):
            try:
                normalized.append(
                    (
                        index,
                        self._normalize_batch_item(
                            raw_item,
                            scope=scope,
                            source=source,
                            tags=tags,
                            system_metadata=system_metadata,
                            user_metadata=user_metadata,
                            occurred_at=occurred_at,
                            stream_id=stream_id,
                        ),
                    )
                )
            except Exception as exc:
                _record_failure(index, raw_item, exc)
                if not continue_on_error:
                    stopped_index = index
                    break

        # 第 2 遍：落点解析（整批一次）与 sequence 去重。两者的次序不可换：去重键含 scope
        # 五维，落点未定时算不出。判定按写入路径生效、不按单条与批量入口区分。
        targets = self._batch_write_targets(normalized, identity, coords)
        for index, item in normalized:
            try:
                outcome = targets.get(index)
                if isinstance(outcome, Exception):
                    raise outcome
                target, item_metadata = outcome
                item = replace(item, scope=target, system_metadata=item_metadata or None)
                if item.sequence is not None:
                    sequence_key = (
                        item.scope.org,
                        item.scope.space,
                        item.scope.user,
                        item.scope.agent,
                        item.scope.session,
                        item.stream_id,
                        item.sequence,
                    )
                    if sequence_key in seen_sequences:
                        raise ValidationError(
                            "duplicate sequence within the same scope and stream"
                        )
                    seen_sequences.add(sequence_key)
                ready.append((index, item))
            except Exception as exc:
                _record_failure(index, items[index], exc)
                if not continue_on_error:
                    stopped_index = index
                    break

        if stopped_index is not None:
            ready = [entry for entry in ready if entry[0] < stopped_index]
            for index in range(stopped_index + 1, len(items)):
                outcomes[index] = BatchWriteOutcome(
                    index=index,
                    item=self._batch_error_item(items[index]),
                    error="skipped after previous item failed",
                    error_type="Skipped",
                )

        authorized: list[tuple[int, BatchWriteItem, dict[str, str]]] = []
        for index, item in ready:
            permission_context = _write_permission_context(
                item.scope, item.tags, item.system_metadata
            )
            try:
                auth = self._authorize(
                    identity,
                    item.scope,
                    Action.WRITE,
                    "add",
                    context=permission_context,
                )
                self._ensure_space_writable(item.scope)
                authorized.append((index, item, auth))
            except (PermissionDeniedError, ValidationError, PolicyError) as exc:
                outcomes[index] = self._batch_outcome(index, item, exc)
                if not isinstance(exc, PermissionDeniedError):
                    self._log(
                        identity,
                        "add",
                        target_scope=item.scope,
                        decision="error",
                        detail={"error": str(exc), "error_type": type(exc).__name__},
                    )
                if not continue_on_error:
                    stopped_index = index
                    break

        if stopped_index is not None:
            for index in range(stopped_index + 1, len(items)):
                outcomes.setdefault(
                    index,
                    BatchWriteOutcome(
                        index=index,
                        item=self._batch_error_item(items[index]),
                        error="skipped after previous item failed",
                        error_type="Skipped",
                    ),
                )
            authorized = [entry for entry in authorized if entry[0] < stopped_index]

        if authorized:
            # 作者标记在鉴权与保留键校验之后写入，逐项各写一份（各项的 metadata 已归并）。
            # 只作用于交给引擎的那份：逐项结果回填的仍是调用方输入的归一化形态，内核标记
            # 是条目内容的一部分，不回显为「调用方传了这些键」。
            engine_result = await self._engine.batch_write(
                [
                    replace(
                        item,
                        system_metadata=_with_author_marks(item.system_metadata, identity),
                    )
                    for _, item, _ in authorized
                ],
                continue_on_error=continue_on_error,
            )
            for engine_outcome, (index, item, auth) in zip(engine_result.outcomes, authorized):
                engine_outcome.index = index
                engine_outcome.item = item
                outcomes[index] = engine_outcome
                self._log(
                    identity,
                    "add",
                    target_scope=item.scope,
                    decision="allow" if not engine_outcome.error else "error",
                    detail={
                        **auth,
                        "error": engine_outcome.error,
                        "error_type": engine_outcome.error_type,
                    },
                )

        return BatchWriteResult(
            outcomes=[outcomes[index] for index in range(len(items))]
        )

    def search(
        self,
        query: str,
        context: Context,
        *,
        security: RequestSecurityContext,
        filters: FilterExpr | list[FilterClause] | dict | None = None,
        as_of: datetime | None = None,
        top_k: int = 10,
        disclosure: DisclosureLevel = DisclosureLevel.L0,
        with_trajectory: bool = False,
    ) -> RetrievalResult:
        """执行检索；``extensions`` 带 ``spaces`` 键时转为跨空间形态（F07「多空间读写」）。

        单空间与跨空间是同一个入口的两条路径，不是两个接口：跨空间不是新的检索算法，是在
        单空间召回之上套的一层编排（定候选空间 → 逐空间判权 → 各自召回 → 轮转合并），
        两族谓词与召回都复用同一份实现。分成两个接口即同一件事有两处契约，接入方须先判断
        部署形态才知道该调哪个。

        ``spaces`` 键不在时本方法与本特性之前逐字一致。判据取键的有无，见 :func:`_pop_spaces`。
        """
        identity = security.auth.actor
        # Context 在边界处拆包：scope 照旧作独立轴下推（鉴权 + 检索），
        # extensions 写入调用级 options 顺 parser 透传给自定义检索模块；
        # Context 对象本身不进内核。三个约定 key 在此解释并从透传 options 中移除，
        # 避免与内核已解释的字段重复：max_tokens（自适应披露预算）解析为 typed int
        # 写入 RetrievalQuery，coords（归属坐标）折算成第二族收窄谓词，spaces（候选空间）
        # 决定走单空间还是跨空间编排。
        options = dict(context.extensions)
        max_tokens = _parse_max_tokens(options.pop(EXT_MAX_TOKENS, None))
        spaces = _pop_spaces(options)
        coords = _pop_coords(options, enabled=self._routing_enabled())
        reject_kernel_coords(coords)
        # 坐标折算成第二族收窄谓词的取值，在此算一次、两条路径共用。放进各自分支即
        # 「两处各折算一次」：漏调哪一处，该路径的 agent 维与 session 维整体不再收窄，
        # 失效方向是放宽且不报错。
        narrow = narrow_dims_of(
            principal.kernel_coords(coords, identity), self._route_table.narrow_dims
        )
        if spaces is not None:
            return self._search_spaces(
                query,
                context,
                identity=identity,
                spaces=spaces,
                filters=filters,
                as_of=as_of,
                top_k=top_k,
                disclosure=disclosure,
                with_trajectory=with_trajectory,
                options=options,
                max_tokens=max_tokens,
                coords=coords,
                narrow=narrow,
            )
        rq = RetrievalQuery(
            text=query,
            # RetrievalQuery 边界统一 normalize 旧 list、clause 与 dict DSL。
            filters=filters,
            as_of=as_of,
            top_k=top_k,
            disclosure=disclosure,
            max_tokens=max_tokens,
            with_trajectory=with_trajectory,
            extensions=options,
        )
        # 权限上下文与 RetrievalQuery 共用同一规范化后的 FilterExpr（不重复转换）。
        permission_context = _recall_permission_context(context, rq.filters)
        auth, auth_context = self._authorize_with_context(
            identity,
            context.scope,
            Action.READ,
            "search",
            context=permission_context,
        )
        # 用户表达式作整体 child 并入外层 AND（与 lifecycle/时间谓词同一机制），不会被
        # 其内部的 OR 稀释。回注的判据见 _routing_clauses_of。
        routing_clauses = _routing_clauses_of(permission_context, self._perm.routing_fields())
        if routing_clauses:
            rq.filters = and_merge(rq.filters, routing_clauses)
        # 两族谓词与调用方表达式合成一个 AND 一次下推，在 top-k 截断之前生效——
        # 召回后二次过滤会让被筛掉的条目白占召回名额，最终返回条数少于 top_k。
        # 第二族由归属坐标折算：坐标缺项不生成对应谓词，表现为该维不收窄，失效方向是放宽。
        system_clauses = space_predicates.system_predicates(
            auth_context.space_facts if auth_context is not None else None,
            identity,
            narrow,
        )
        if system_clauses:
            rq.filters = and_merge(rq.filters, system_clauses)
        result = asyncio.run(self._engine.recall(context.scope, rq))
        self._log(identity, "search", target_scope=context.scope, detail=auth)
        return result

    def _search_spaces(
        self,
        query: str,
        context: Context,
        *,
        identity: Scope,
        spaces: list[str],
        filters: FilterExpr | list[FilterClause] | dict | None,
        as_of: datetime | None,
        top_k: int,
        disclosure: DisclosureLevel,
        with_trajectory: bool,
        options: dict[str, Any],
        max_tokens: int | None,
        coords: dict[str, str] | None,
        narrow: dict[str, str],
    ) -> RetrievalResult:
        """跨空间检索：本层做前两步半，后三步下沉控制层（F07「多空间读写」）。

        由 :meth:`search` 在 ``extensions`` 带 ``spaces`` 键时分流进来，不是独立入口。
        参数袋的拆包与坐标折算都在 :meth:`search` 内完成，结果经形参传入：同一个参数袋
        解释两遍，两处的解释一旦分叉即出现「同一次调用两套取值」。``coords`` 只用于日志，
        实际生效的是已折算好的 ``narrow``。

        | 步 | 内容 | 落点 |
        |---|---|---|
        | 1 定候选空间 | 显式 ``spaces`` 或主体反查索引 | 本层（反查按 ``identity``） |
        | 2 逐空间判权与状态校验 | ``PermissionManager.decide`` | 本层（循环体就是 PEP） |
        | 2.5 逐空间谓词 | 路由值回注 + 两族系统谓词 | 本层（按 ``identity`` 与空间事实） |
        | 3—5 摊配、扇出、合并 | 取数上界、逐空间召回、轮转合并 | 控制层 ``cross_space_recall`` |

        分界取 S02「不做业务编排逻辑」那条的原文判据——「移出本层是否还能按 ``identity``
        裁决」。前两步半读 ``identity``，移出即把 PEP 分裂为两处；后三步在候选集与谓词都
        已定妥之后执行，全程不需要知道调用方是谁，留在本层只是让 PEP 多背一段取数编排。

        第 2 步不放进协程：事实读取与判权都是同步调用，授权记录查询还会在存储层串行。
        判权前置的副产品是取数上界按实际可读空间数计算，无权空间不再凭空占用召回名额。

        ``context.scope`` 只取 ``org`` 维定组织边界，空间维由候选集给出、传了不生效。
        """
        principal.require_principal(identity)
        org = context.scope.org
        normalized_filters = normalize(filters)

        candidates = self._search_candidates(identity, org, spaces)
        # 收窄谓词的实际取值只在此处成形，下游只能看到条数。缺这一行时「召回为空」
        # 无法区分坐标未传到、判定表未声明该维、以及该维确实过滤掉了全部条目。
        logger.info(
            "search cross-space: query=%r coords=%s narrow=%s candidate_spaces=%s",
            query[:60],
            coords,
            narrow,
            candidates,
        )
        targets: list[collective.SpaceRecallTarget] = []
        denied: list[ChannelError] = []
        for space in candidates:
            target = Scope(org=org, space=space)
            permission_context, facts = self._apply_space_policy_context(
                target,
                _recall_permission_context(
                    Context(scope=target, extensions=dict(context.extensions)),
                    normalized_filters,
                ),
                entry="search",
            )
            try:
                outcome = self._perm.decide(
                    identity, target, Action.READ, context=permission_context
                )
                if outcome.allowed and self._needs_space_facts():
                    # 状态校验与单空间路径同一口径（F07「空间状态校验」）：不补则
                    # 调用方在 extensions["spaces"] 里点名一个正在清理的空间即可照常
                    # 拿到内容，同一个 search 的两条路径对该空间给出两种结果。
                    # 排在判权通过之后、复用同一份事实快照，与 _authorize_with_context
                    # 一致；`_needs_space_facts` 这层门控同样不可省——未装配空间治理的
                    # 部署里无条件加会收紧既有行为。
                    self._ensure_space_state_allows(
                        target,
                        Action.READ,
                        "search",
                        info=facts.info if facts is not None else None,
                    )
            except (PermissionDeniedError, BackendError, NotFoundError, ValidationError) as exc:
                denied.append(_space_denied(space, type(exc).__name__, str(exc)))
                continue
            if outcome.allowed:
                # 第 2.5 步：本空间专属的系统谓词。与单空间入口同一处理——授权所依据的
                # 路由值回注，再叠两族系统谓词。逐空间各算自己的那份：各空间的授权可以
                # 来自不同的策略，共用一份即某个空间按另一个空间的授权取数。
                #
                # 这一步留本层而不随扇出一起下沉：两族谓词由 ``identity`` 与该空间的事实
                # 生成，是 S02「鉴权驱动的编排」明列的一项（生成并回注系统谓词）。
                clauses = _routing_clauses_of(permission_context, self._perm.routing_fields())
                clauses.extend(
                    space_predicates.system_predicates(
                        permission_context.space_facts, identity, narrow
                    )
                )
                targets.append(
                    collective.SpaceRecallTarget(scope=target, clauses=tuple(clauses))
                )
            else:
                denied.append(
                    _space_denied(
                        space,
                        PermissionDeniedError.__name__,
                        f"read denied: rule={outcome.rule} reason={outcome.reason}",
                    )
                )
        # 候选集非空而一个都读不到时抛，与单空间路径同一处置：那条路径上无权即
        # PermissionDeniedError，本路径若静默返回空，同一个方法的两条路径对「完全无权」
        # 给出两种结果，且「无权」与「这些空间里没有内容」在调用方看来不可区分。
        # 候选集为空不抛——那是「主体不在任何空间里」，是合法的空结果。
        if candidates and not targets:
            # 候选来自调用方显式传入时回显空间名（那是他自己的入参，便于排查）；来自主体
            # 反查索引时只给条数。索引按 `context.scope.org` 建桶，而该 org 取自参数袋、
            # 与 `identity.org` 无一致性校验——回显即把另一个组织的空间名交给调用方，而
            # 逐空间判权只挡住了访问，挡不住这行措辞。
            detail = repr(candidates) if spaces else f"{len(candidates)} space(s)"
            raise PermissionDeniedError(f"read denied on every candidate space: {detail}")

        # 查询骨架装配一次，逐空间只差取数上界与本空间谓词，两项都由控制层补齐。
        # 逐空间构造 RetrievalQuery 的循环本身就是取数编排，不留本层。
        # 同步桥接留本层（S02「同步/异步桥接」）：控制层给协程，本层 asyncio.run。
        merged, space_failures = asyncio.run(
            collective.recall_spaces(
                targets,
                RetrievalQuery(
                    text=query,
                    filters=normalized_filters,
                    as_of=as_of,
                    disclosure=disclosure,
                    max_tokens=max_tokens,
                    with_trajectory=with_trajectory,
                    extensions=options,
                ),
                top_k=top_k,
                recall=self._engine.recall,
            )
        )
        for failure in space_failures:
            # 扇出失败逐空间落一条审计。判据取分离返回的那份列表，不从 merged.errors 里
            # 按 channel 过滤——后者会把审计范围绑在控制层的 channel 编码上。
            self._log(
                identity,
                "search",
                failure.source,
                target_scope=Scope(org=org, space=failure.source),
                decision="error",
                detail={"entry": "cross_space", "error": failure.message},
            )
        # 判权剔除与扇出失败一并进 errors，不静默丢弃：调用方拿到少于预期的结果时，
        # 「这个空间我读不到」「这个空间挂了」「这个空间里没有内容」三者的后续动作完全
        # 不同，而只记审计日志时它们在返回值上是同一形态。三类共用 ChannelError，按
        # source 区分是哪个空间、按 error_type 区分是哪一类。各空间自己的分通道错误
        # （VECTOR / KEYWORD 等）已由控制层的合并并入。
        merged.errors.extend(denied + space_failures)
        self._log(
            identity,
            "search",
            org,
            target_scope=Scope(org=org),
            detail={
                "entry": "cross_space",
                "candidate_spaces": str(len(candidates)),
                "readable_spaces": str(len(targets)),
                "denied_spaces": str(len(denied)),
                "failed_spaces": str(len(space_failures)),
                "count": str(len(merged.items)),
            },
        )
        return merged

    def _search_candidates(self, identity: Scope, org: str, spaces: list[str]) -> list[str]:
        """第 1 步：定候选空间。``spaces`` 非空就用它，为空则取主体反查索引结果。

        反查索引是超集契约——不遗漏、允许多给，权限由第 2 步的逐空间判权裁决。截断记
        WARNING，不静默丢弃：静默截断读起来与「这些空间里确实没有内容」不可区分。
        """
        if spaces:
            candidates = [space for space in dict.fromkeys(spaces) if space]
        elif self._membership is not None:
            candidates = list(self._membership.spaces_for(identity, org))
        else:
            candidates = []
        limit = self._space_fanout_limit()
        if len(candidates) > limit:
            self._log(
                identity,
                "search",
                org,
                target_scope=Scope(org=org),
                decision="allow",
                detail={
                    "entry": "cross_space",
                    "truncated_spaces": str(len(candidates) - limit),
                },
            )
            candidates = candidates[:limit]
        return candidates

    def list(
        self,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, Any] | None = None,
        filters: FilterExpr | list[FilterClause] | dict | None = None,
    ) -> MemoryListResult:
        identity = security.auth.actor
        normalized_extensions = _normalize_list_extensions(extensions)
        normalized_filters = normalize(filters)
        permission_contexts = _list_permission_contexts(
            scope,
            memory_types,
            normalized_filters,
            normalized_extensions,
        )
        auth: dict[str, str] = {}
        auth_context: PermissionContext | None = None
        for permission_context in permission_contexts:
            auth, auth_context = self._authorize_with_context(
                identity,
                scope,
                Action.READ,
                "list",
                context=permission_context,
            )
        routing_clauses = _list_routing_clauses(
            permission_contexts,
            self._perm.routing_fields(),
            memory_types,
        )
        effective_filters = and_merge(normalized_filters, routing_clauses)
        # list 必须注入第一族，否则个体空间的隔离只在 search 上成立：它是同样按空间返回
        # 条目的批量入口，不注入的后果与 search 同因同向。第二段逐条鉴权不能替代谓词——
        # 逐条鉴权的失败形态是抛异常而非过滤，整次调用失败而不是少返回几条。
        system_clauses = _first_family_predicate(identity, auth_context)
        if system_clauses:
            effective_filters = and_merge(effective_filters, system_clauses)
        result, unit_contexts = asyncio.run(
            self._engine.list_with_permission_contexts(
                scope,
                offset=offset,
                limit=limit,
                memory_types=memory_types,
                extensions=normalized_extensions,
                filters=effective_filters,
            )
        )
        for permission_context in unit_contexts:
            # 第二段不携带作者标记：逐条鉴权的失败形态是抛异常而非过滤，携带后个体空间内
            # 只要有一条作者不是调用方的条目，整次调用即失败——代理自主运行写入的条目与
            # 回填期多归属空间中另一归属主体写入的条目都属此列。内容边界由第一族谓词在
            # 取数时承担，本段只判条目真源 scope 的空间归属。
            auth = self._authorize(
                identity,
                permission_context.scope,
                Action.READ,
                "list",
                permission_context.unit_id,
                context=permission_context,
                carry_author_marks=False,
            )
        if len(permission_contexts) > 1:
            auth["permission_memory_types"] = ",".join(
                context.memory_type for context in permission_contexts
            )
        self._log(
            identity,
            "list",
            target_scope=scope,
            detail={
                **auth,
                "count": str(result.count),
                "page_count": str(len(result.items)),
            },
        )
        return result

    def get(
        self,
        unit_id: str,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        as_of: datetime | None = None,
    ) -> MemoryUnit:
        identity = security.auth.actor
        self._authorize(
            identity,
            scope,
            Action.READ,
            "get",
            unit_id,
            context=_unit_lookup_permission_context(unit_id, scope),
        )
        permission_context = asyncio.run(self._engine.permission_context_for_unit(unit_id, scope))
        # 第二段的目标取条目真源 scope，不沿用入参（F07「条目级入口分两段鉴权」）：
        # 它是「判定第 8 步不会命中」的两个前置条件之一，与「回填后条目 scope 只有两维」
        # 各兜一重，两条的失效方向相反。沿用入参即把两重约束落在同一个取值上。
        # 与 list / delete 同一口径。
        auth = self._authorize(
            identity,
            permission_context.scope,
            Action.READ,
            "get",
            unit_id,
            context=permission_context,
        )
        unit = asyncio.run(self._engine.get(unit_id, scope, as_of))
        self._log(
            identity,
            "get",
            unit_id,
            target_scope=scope,
            detail={**auth, "after_unit_id": unit.id},
        )
        return unit

    def update(
        self,
        unit_id: str,
        scope: Scope,
        patch: MemoryPatch,
        *,
        security: RequestSecurityContext,
    ) -> MemoryUnit:
        identity = security.auth.actor
        _reject_kernel_system_metadata(patch.system_metadata)
        # 判定标签键在改写入口同样不可由调用方赋值：只挂写入入口时，改写即绕过通道——
        # 内容 EDITOR 可把他人条目的会话标签改成自己的会话 id，使其出现在他人的上下文里，
        # 或改写项目标签使条目脱离按 `system_metadata.<tag_key>` 谓词执行的批量删除范围。
        # 入参 scope 的主体维不在此校验：它是条目查找键而非落盘键。
        _reject_route_tag_keys(patch.system_metadata, self._route_table.tag_keys)
        _reject_non_scalar_metadata(patch.system_metadata, field_name="system_metadata")
        _reject_non_scalar_metadata(patch.user_metadata, field_name="user_metadata")
        self._authorize(
            identity,
            scope,
            Action.UPDATE,
            "update",
            unit_id,
            context=_unit_lookup_permission_context(unit_id, scope),
        )
        permission_context = asyncio.run(self._engine.permission_context_for_unit(unit_id, scope))
        # 第二段的目标取条目真源 scope，理由同 get。随后的 _ensure_space_writable 仍取
        # 入参 scope——它校验的是本次写入落点所在空间，与鉴权目标是两件事。
        auth = self._authorize(
            identity,
            permission_context.scope,
            Action.UPDATE,
            "update",
            unit_id,
            context=permission_context,
        )
        self._ensure_space_writable(scope)
        before = asyncio.run(self._engine.get(unit_id, scope, None))
        unit = asyncio.run(self._engine.update(unit_id, scope, patch))
        self._log(
            identity,
            "update",
            unit_id,
            target_scope=scope,
            detail={**auth, "before_unit_id": before.id, "after_unit_id": unit.id},
        )
        return unit

    def delete(self, selector: DeleteSelector, *, security: RequestSecurityContext) -> list[str]:
        identity = security.auth.actor
        selector_is_empty = (
            not selector.unit_ids
            and not selector.tags
            and selector.before is None
            and selector.filters is None
        )
        if selector_is_empty:
            raise ValidationError("DeleteSelector requires unit_ids, tags, before, or filters")
        # 按 selector 的目标 scope 鉴权 DELETE；未限定 scope（如纯按 id/标签的
        # 跨范围删除）则退到根 scope 闸门，要求更高权限。
        target = selector.scope or _ROOT
        selector_context = _selector_permission_context(selector, target)
        if selector.scope is not None or not selector.unit_ids:
            self._authorize(
                identity,
                target,
                Action.DELETE,
                "delete",
                context=selector_context,
            )
        contexts = asyncio.run(self._engine.permission_contexts_for_delete(selector))
        if not contexts:
            auth = self._authorize(
                identity,
                target,
                Action.DELETE,
                "delete",
                context=selector_context,
            )
        else:
            auth = {"permission_check": "enabled", "permission_reason": "permission check passed"}
            for permission_context in contexts:
                unit_auth = self._authorize(
                    identity,
                    permission_context.scope,
                    Action.DELETE,
                    "delete",
                    permission_context.unit_id,
                    context=permission_context,
                )
                auth.update(unit_auth)
        deleted = asyncio.run(self._engine.delete(selector))
        self._log(
            identity,
            "delete",
            target_scope=target,
            detail={**auth, "before_unit_ids": json.dumps(deleted, ensure_ascii=False)},
        )
        return deleted

    def evolve(
        self,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel = Channel.BACKGROUND,
        *,
        security: RequestSecurityContext,
    ) -> str:
        identity = security.auth.actor
        auth = self._authorize(
            identity,
            scope,
            Action.WRITE,
            "evolve",
            space_action=_evolve_space_action(mode),
        )
        self._ensure_space_writable(scope)
        job_id = asyncio.run(self._engine.evolve(scope, mode, channel))
        self._log(identity, "evolve", target_scope=scope, detail={**auth, "job_id": job_id})
        return job_id

    # -- 任务调度（直达 Scheduler） ----------------------------------------- #

    def job_status(
        self,
        job_id: str,
        *,
        security: RequestSecurityContext,
        scope: Scope | None = None,
    ) -> JobInfo:
        identity = security.auth.actor
        if job_id.startswith(INGEST_JOB_PREFIX):
            if scope is None:
                raise ValidationError("ingest job status requires target scope")
            auth = self._authorize(
                identity,
                scope,
                Action.READ,
                "job_status",
                job_id,
                space_action=_evolve_space_action("ingest"),
            )
            job = self._ingest_jobs.status(job_id, scope=scope, owner=identity)
            info = JobInfo(
                id=job.id,
                channel=Channel.BACKGROUND,
                mode="ingest",
                scope=job.scope,
                status=JobStatus(job.status),
                detail={
                    "payload_id": job.payload_id,
                    "source_ref": job.source_ref,
                    "unit_ids": ",".join(job.unit_ids),
                    "error": job.error,
                },
            )
        else:
            info = self._scheduler.status(job_id)
            auth = self._authorize(
                identity,
                info.scope,
                Action.READ,
                "job_status",
                job_id,
                space_action=_evolve_space_action(info.mode),
            )
        self._log(identity, "job_status", job_id, target_scope=info.scope, detail=auth)
        return info

    def job_cancel(self, job_id: str, *, security: RequestSecurityContext) -> None:
        identity = security.auth.actor
        # 取消即对该任务范围的写动作，按其 scope 鉴权 WRITE
        # （与 evolve 触发一致）。
        info = self._scheduler.status(job_id)
        auth = self._authorize(
            identity,
            info.scope,
            Action.WRITE,
            "job_cancel",
            job_id,
            space_action=_evolve_space_action(info.mode),
        )
        self._log(identity, "job_cancel", job_id, target_scope=info.scope, detail=auth)
        self._scheduler.cancel(job_id)

    # -- admin（直达 PolicyManager；管理面闸门 = 根 scope 鉴权） ------------- #

    def admin_get(self, key: str, *, security: RequestSecurityContext) -> str:
        identity = security.auth.actor
        auth = self._authorize(identity, _ROOT, Action.READ, "admin_get", key)
        self._log(identity, "admin_get", key, target_scope=_ROOT, detail=auth)
        return self._policy.get(key)

    def admin_set(self, key: str, value: str, *, security: RequestSecurityContext) -> None:
        identity = security.auth.actor
        auth = self._authorize(identity, _ROOT, Action.WRITE, "admin_set", key)
        self._log(identity, "admin_set", key, target_scope=_ROOT, detail=auth)
        self._policy.set(key, value)

    def admin_all(self, *, security: RequestSecurityContext) -> dict[str, str]:
        identity = security.auth.actor
        auth = self._authorize(identity, _ROOT, Action.READ, "admin_all")
        self._log(identity, "admin_all", target_scope=_ROOT, detail=auth)
        return self._policy.all()

    # -- 治理（直达 Governor） ---------------------------------------------- #

    def inspect(
        self, unit_ids: list[str], scope: Scope, *, security: RequestSecurityContext
    ) -> list[MemoryUnit]:
        identity = security.auth.actor
        auth = self._authorize(identity, scope, Action.READ, "inspect")
        self._log(identity, "inspect", target_scope=scope, detail=auth)
        return self._governor.inspect(unit_ids, scope)

    def trace(
        self, unit_id: str, scope: Scope, *, security: RequestSecurityContext
    ) -> list[MemoryUnit]:
        identity = security.auth.actor
        auth = self._authorize(identity, scope, Action.READ, "trace", unit_id)
        self._log(identity, "trace", unit_id, target_scope=scope, detail=auth)
        return self._governor.trace(unit_id, scope)

    def audit(
        self,
        filters: dict[str, str],
        *,
        security: RequestSecurityContext,
        limit: int = 100,
    ) -> list[AuditEvent]:
        identity = security.auth.actor
        # 审计查询跨 scope，继续按既有管理面闸门（根 scope READ）鉴权；存量授权记录
        # 按 action 精确匹配，本接口 PR 不迁移其语义。READ_AUDIT 的切换须由独立的
        # 兼容性变更连同授权数据迁移一起完成。查询本身亦留痕。
        auth = self._authorize(identity, _ROOT, Action.READ, "audit")
        self._log(identity, "audit", target_scope=_ROOT, detail=auth)
        return self._governor.audit(filters, limit)

    def verify_audit(
        self,
        *,
        security: RequestSecurityContext,
        after_sequence: int = 0,
        page_size: int = DEFAULT_AUDIT_VERIFY_PAGE_SIZE,
        max_samples: int = DEFAULT_AUDIT_VERIFY_MAX_SAMPLES,
        anchor_policy: str = "if_configured",
    ) -> AuditVerificationResult:
        identity = security.auth.actor
        if (
            not isinstance(after_sequence, int)
            or isinstance(after_sequence, bool)
            or after_sequence < 0
        ):
            raise ValidationError("after_sequence must be a non-negative integer")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
            raise ValidationError("page_size must be a positive integer")
        if not isinstance(max_samples, int) or isinstance(max_samples, bool) or max_samples < 0:
            raise ValidationError("max_samples must be a non-negative integer")
        effective_page_size = min(page_size, self._audit_verify_limits.max_page_size)
        effective_max_samples = min(max_samples, self._audit_verify_limits.max_samples)
        if anchor_policy not in {"if_configured", "required", "skip"}:
            raise ValidationError(
                f"anchor_policy must be one of if_configured/required/skip, got {anchor_policy!r}"
            )
        # 验证审计链完整性：管理面根 scope 闸门使用独立 VERIFY_AUDIT 动作；
        # 验证本身亦留痕。
        auth = self._authorize(identity, _ROOT, Action.VERIFY_AUDIT, "verify_audit")
        if self._audit_integrity is None:
            # 未装配审计完整性 provider：诚实返回 unsupported，不抛错。
            self._log(identity, "verify_audit", target_scope=_ROOT, detail=auth)
            return AuditVerificationResult(
                status=AuditIntegrityStatus.UNSUPPORTED,
                checked_count=0,
                error_count=0,
                truncated=False,
                high_water_mark=0,
                key_epoch_range=(0, 0),
                anchor=AnchorState(checked=False),
                detail="audit integrity provider not configured",
            )
        # 全量验证在 WorkloadGuard 独立预算下执行：占用一个并发槽，耗尽则拒绝。
        guard = self._audit_verify_guard
        if guard is None:
            raise ValidationError("audit verify workload guard is not configured")
        if not guard.acquire():
            self._log(
                identity,
                "verify_audit",
                target_scope=_ROOT,
                # decision 表达授权结果；此处授权已通过，失败的是操作准入，不能让
                # decision=deny 的安全事件筛选把容量不足误报成权限拒绝。
                decision="allow",
                detail={
                    **auth,
                    "workload_guard": "exhausted",
                },
            )
            raise RateLimitedError("audit verification workload budget exhausted")
        # 与 audit() 一致，真正读取审计数据前先记录本次已授权且已准入的验证尝试。
        # provider 若因链篡改、schema 损坏等抛 AuditIntegrityError，此记录仍可追溯
        # 发起者与发生时间；异常继续原样传播，guard 仍由 finally 归还。
        self._log(identity, "verify_audit", target_scope=_ROOT, detail=auth)
        try:
            result = self._audit_integrity.verify(
                after_sequence=after_sequence,
                page_size=effective_page_size,
                max_samples=effective_max_samples,
                anchor_policy=anchor_policy,
            )
        finally:
            guard.release()
        # Provider 也受契约约束，但 PEP 对公网返回体再做一次 fail-safe 截断；自定义或
        # 旧 provider 即使错误地返回过多样本，也不能突破本次请求和可信装配的有效上限。
        if len(result.samples) > effective_max_samples:
            result = replace(
                result,
                samples=result.samples[:effective_max_samples],
                truncated=True,
            )
        return result

    # -- 跨 scope 授权（直达 PermissionManager） ---------------------------- #

    def grant(self, grant: Grant, *, security: RequestSecurityContext) -> Grant:
        identity = security.auth.actor
        auth = self._authorize(identity, grant.grantor, Action.SHARE, "grant")
        self._enforce_grant_ceiling(identity, grant)
        self._log(identity, "grant", target_scope=grant.grantor, detail=auth)
        # 旧 PermissionManager 尚不按 grant_id 定位，因此本期不生成 ID
        #（返回值原样回传，grant_id 保持入参值）。
        # 服务端生成 ID 与按 ID 定位随 GrantStore 实装一并落地。
        # PermissionManager 与安全域共用同一 Grant/Action 类型；管理动作在旧实现
        # 尚无角色闸门，必须先显式拒绝，不能借旧 ACL 语义放行。
        _validate_legacy_permission_actions(grant)
        self._perm.grant(grant)
        return grant

    def revoke(self, grant: Grant, *, security: RequestSecurityContext) -> None:
        identity = security.auth.actor
        auth = self._authorize(identity, grant.grantor, Action.SHARE, "revoke")
        self._log(identity, "revoke", target_scope=grant.grantor, detail=auth)
        # 旧 PermissionManager 按 grantor+grantee+action 条件撤销，不能按 grant_id
        # 定位。本期不据 grant_id 做任何判定，也不宣称精确撤销；
        # 契约要求的「按 ID 精确回收」随 GrantStore 实装落地。
        _validate_legacy_permission_actions(grant)
        self._perm.revoke(grant)

    # -- Space 管理（直达 SpaceManager） ------------------------------------ #

    def create_space(self, spec: SpaceSpec, *, security: RequestSecurityContext) -> SpaceInfo:
        identity = security.auth.actor
        target = _space_scope(spec.org, spec.space)
        target_id = _space_target_id(spec.org, spec.space)
        auth = self._authorize(
            identity,
            Scope(org=spec.org),
            Action.WRITE,
            "create_space",
            target_id,
            context=_space_permission_context("space", target),
            require_space=False,
        )
        info = self._space.create(_resolve_space_owner(spec, identity))
        # 建空间同样要下发失效：事实缓存对「空间不存在」也装填一份（元数据与成员皆空），
        # 建之前任何一次读取（含 get_space 的鉴权路径）都会装填它，不清则新空间在一个
        # TTL 内判定无归属、无成员，归属主体本人也写不进去。
        self._invalidate_space_facts(spec.org, spec.space)
        self._log(identity, "create_space", target_id, target_scope=target, detail=auth)
        return info

    def get_space(self, org: str, space: str, *, security: RequestSecurityContext) -> SpaceInfo:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "get_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        info = _trim_space_policy(self._space.get(org, space), auth.get("permission_axis", ""))
        self._log(identity, "get_space", target_id, target_scope=target, detail=auth)
        return info

    def list_spaces(
        self,
        org: str,
        *,
        security: RequestSecurityContext,
        status: SpaceStatus | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[SpaceInfo]:
        identity = security.auth.actor
        target = Scope(org=org)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "list_spaces",
            org,
            context=_space_permission_context("space_list", target),
            require_space=False,
            check_permission=not self._needs_space_facts(),
        )
        detail = dict(auth)
        if self._needs_space_facts():
            if limit <= 0:
                raise ValidationError("limit must be > 0")
            candidates = self._listable_candidates(org, status)
            spaces = self._readable_spaces(identity, org, candidates)[:limit]
            detail["candidate_spaces"] = str(len(candidates))
            if cursor is not None:
                # F07「``cursor`` 标记废弃」：``limit`` 在鉴权之后生效，偏移量按候选序解释
                # 会与返回序错位。忽略但记一笔——静默忽略会让期望翻页的调用方拿到重复页
                # 而无从察觉。
                detail["cursor_ignored"] = str(cursor)
        else:
            spaces = self._space.list(org, status=status, limit=limit, cursor=cursor)
        self._log(
            identity,
            "list_spaces",
            org,
            target_scope=target,
            detail={**detail, "count": str(len(spaces))},
        )
        return spaces

    def _listable_candidates(self, org: str, status: SpaceStatus | None) -> list[SpaceInfo]:
        """``list_spaces`` 的候选：org 下的全部空间，交由逐空间判权裁决。

        **不取主体反查索引。** 索引的超集契约针对成员关系——写入方只有归属登记与成员记录
        两类，靠显式授权（``grant``）取得读权的主体不在索引里。以索引作候选，这类调用方
        直接 ``search`` 读得到、``list_spaces`` 却列不出来，且不报错。与 F07 决策 23
        「不以反查索引粗筛写入候选」是同一条理由，读侧同样成立。

        授权记录接入索引不在本批处置：索引项不带来源标记，``remove_member`` 已需靠
        ``normalized not in info.owners`` 这一显式来源判断才能避免误删归属主体的索引项；
        再加第三类写入方，``revoke`` 与 ``remove_member`` 两个方向都要跨组件判断「该主体
        是否仍凭另一来源持有该空间」，而授权记录在 :class:`PermissionManager` 里，
        :class:`SpaceManager` 查不到。判断不全的失效方向是索引遗漏，恰是契约唯一禁止的
        方向。前置条件与遗留事项见 F07「``list_spaces`` 的候选来源」。

        代价是判权次数等于 org 下的空间数。这是既有形态而非本次引入；取值上界由
        :data:`_SPACE_SCAN_CAP` 封住，达到上界记 WARNING，不静默截断。
        """
        infos = self._space.list(org, status=status, limit=_SPACE_SCAN_CAP)
        if len(infos) >= _SPACE_SCAN_CAP:
            logger.warning(
                "list_spaces: org %s has at least %d spaces, candidates truncated at the scan cap",
                org,
                _SPACE_SCAN_CAP,
            )
        return infos

    def _readable_spaces(
        self, identity: Scope, org: str, spaces: list[SpaceInfo]
    ) -> list[SpaceInfo]:
        """逐空间求值，无权的直接剔除（F07「跨空间检索」末段）。

        整段在返回之前一次性完成，走与单空间入口同一个鉴权方法——分叉即出现「列得出
        但打不开」或其反向。

        逐空间的拒绝不落审计：一次调用对无权空间产生 M 条拒绝记录，审计价值低于噪声
        成本；整次调用仍记一条。

        策略裁剪与 ``get_space`` 同判据同实现：两者同处归属主体档第二级，分叉即出现
        「列表里读得到、单查读不到」或其反向。
        """
        readable: list[SpaceInfo] = []
        for info in spaces:
            target = _space_scope(org, info.space)
            context, _ = self._apply_space_policy_context(
                target,
                _space_permission_context("space", target),
                entry="list_spaces",
            )
            outcome = self._perm.decide(identity, target, Action.READ, context=context)
            if outcome.allowed:
                axis = outcome.axis.value if outcome.axis is not None else ""
                readable.append(_trim_space_policy(info, axis))
        return readable

    def update_space(
        self,
        org: str,
        space: str,
        patch: SpacePatch,
        *,
        security: RequestSecurityContext,
    ) -> SpaceInfo:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.UPDATE,
            "update_space",
            target_id,
            context=_space_permission_context("space", target),
            space_patch=patch,
        )
        info = self._space.update(org, space, patch)
        self._invalidate_space_facts(org, space)
        self._log(identity, "update_space", target_id, target_scope=target, detail=auth)
        return info

    def archive_space(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> SpaceInfo:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.UPDATE,
            "archive_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        info = self._space.archive(org, space)
        self._invalidate_space_facts(org, space)
        self._log(identity, "archive_space", target_id, target_scope=target, detail=auth)
        return info

    def delete_space(
        self,
        org: str,
        space: str,
        *,
        security: RequestSecurityContext,
        mode: DeleteMode = DeleteMode.PURGE,
    ) -> SpaceDeleteResult:
        identity = security.auth.actor
        if mode != DeleteMode.PURGE:
            raise ValidationError("delete_space currently supports DeleteMode.PURGE only")
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.DELETE,
            "delete_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        purged = self._purge_space_memories(target)
        result = self._space.delete(org, space)
        self._invalidate_space_facts(org, space)
        result.deleted_counts["memory"] = result.deleted_counts.get("memory", 0) + len(purged)
        result.deleted_counts["index"] = result.deleted_counts.get("index", 0) + len(purged)
        result.deleted_counts["kv"] = result.deleted_counts.get("kv", 0) + len(purged)
        self._log(
            identity,
            "delete_space",
            target_id,
            target_scope=target,
            detail={
                **auth,
                "deleted_memory_ids": json.dumps(purged, ensure_ascii=False),
                "deleted_counts": json.dumps(result.deleted_counts, ensure_ascii=False),
            },
        )
        return result

    def export_space(
        self,
        org: str,
        space: str,
        *,
        security: RequestSecurityContext,
        include_audit: bool = True,
    ) -> str:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "export_space",
            target_id,
            context=_space_permission_context("space_export", target),
        )
        export_id = self._space.export(org, space, include_audit=include_audit)
        self._log(
            identity,
            "export_space",
            target_id,
            target_scope=target,
            detail={**auth, "export_id": export_id, "include_audit": str(include_audit)},
        )
        return export_id

    def space_usage(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> SpaceUsage:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "space_usage",
            target_id,
            context=_space_permission_context("space_usage", target),
        )
        usage = self._space.usage(org, space)
        self._log(
            identity,
            "space_usage",
            target_id,
            target_scope=target,
            detail={
                **auth,
                "memory_count": str(usage.memory_count),
                "message_count": str(usage.message_count),
                "storage_bytes": str(usage.storage_bytes),
            },
        )
        return usage

    def get_space_policy(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> SpacePolicy:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "get_space_policy",
            target_id,
            context=_space_permission_context("space_policy", target),
        )
        policy = self._space.get_policy(org, space)
        self._log(identity, "get_space_policy", target_id, target_scope=target, detail=auth)
        return policy

    def set_space_policy(
        self,
        org: str,
        space: str,
        policy: SpacePolicy,
        *,
        security: RequestSecurityContext,
    ) -> SpacePolicy:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.UPDATE,
            "set_space_policy",
            target_id,
            context=_space_permission_context("space_policy", target),
        )
        updated = self._space.set_policy(org, space, policy)
        self._invalidate_space_facts(org, space)
        self._log(
            identity,
            "set_space_policy",
            target_id,
            target_scope=target,
            detail={**auth, "principal_path": updated.principal_path.value},
        )
        return updated

    def list_space_members(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> list[SpaceMember]:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "list_space_members",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        members = self._space.list_members(org, space)
        self._log(
            identity,
            "list_space_members",
            target_id,
            target_scope=target,
            detail={**auth, "count": str(len(members))},
        )
        return members

    def add_space_member(
        self,
        org: str,
        space: str,
        member: SpaceMember,
        *,
        security: RequestSecurityContext,
    ) -> None:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.SHARE,
            "add_space_member",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        self._enforce_member_write_ceilings(identity, target, member)
        self._space.add_member(org, space, member)
        self._invalidate_space_facts(org, space)
        self._log(
            identity,
            "add_space_member",
            target_id,
            target_scope=target,
            detail={**auth, "member_role": member.role},
        )

    def remove_space_member(
        self,
        org: str,
        space: str,
        member: Scope,
        *,
        security: RequestSecurityContext,
    ) -> None:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.SHARE,
            "remove_space_member",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        self._enforce_member_removal_ceiling(identity, target, member)
        self._space.remove_member(org, space, member)
        self._invalidate_space_facts(org, space)
        self._log(identity, "remove_space_member", target_id, target_scope=target, detail=auth)

    # -- 鉴权 + 审计公共点 --------------------------------------------------- #
