"""空间感知判定的宿主（S09，F03 决策 4）。

与 ``sqlite`` / ``allow_all`` / ``routing`` 平级注册的 ``PermissionManager`` 实现。

**本模块只做适配，不含判据。** 全部判据在 :mod:`common.security.space_decision`，是不
访问存储的纯函数。本模块负责三件事：从权限上下文取出判定输入、查显式授权记录、把判定
结论折算成 ``PermissionManager`` 契约要求的布尔值。判据与宿主分离使换宿主只改本模块，
判据不动。

十步判定中的第 1、3、4、5、9 步在本特性内无判据来源（组织级角色与委托记录不属本特性
范围）。除第 3 步外均为「命中即放行」分支，缺失方向是更严格而非更宽松。第 3 步是终局
判据，其缺失会使组织级入口落入后续步骤而被拒，因此本实现对未登记入口与组织级入口一律
回落父类判据。
"""

from __future__ import annotations

from jiuwen_memory.common.security.principal import AUTHOR_PRINCIPAL
from jiuwen_memory.common.security.space_decision import (
    ATTR_PRINCIPAL_PATH,
    ATTR_SPACE_ACTION,
    ATTR_SPACE_AXIS,
    ATTR_SPACE_ENTRY,
    DecisionOutcome,
)
from jiuwen_memory.common.security.space_decision import (
    decide as decide_axis,
)
from jiuwen_memory.common.security.space_roles import (
    ENTRY_RULES,
    SpaceAction,
    SpaceAxis,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.permission import PermissionProducer
from jiuwen_memory.control.types import Action, PermissionContext

from .sqlite_permission_manager import SQLitePermissionManager, scope_covers

# 空间动作枚举到授权记录动作的映射。显式授权只在内容轴参与求值，而内容轴用到的五个
# 动作在控制层五值枚举中都有同名项；治理轴独有的 REVOKE_SHARE 与三个组织级动作不参与，
# 因此不在表内，查授权记录时按「无对应动作」处置。
_GRANT_ACTIONS: dict[SpaceAction, Action] = {
    SpaceAction.READ: Action.READ,
    SpaceAction.WRITE: Action.WRITE,
    SpaceAction.UPDATE: Action.UPDATE,
    SpaceAction.DELETE: Action.DELETE,
    SpaceAction.SHARE: Action.SHARE,
}


class SpaceAwarePermissionManager(SQLitePermissionManager):
    """两轴角色、归属对比与归属主体档的判定实现。

    继承 SQLite 实现而非组合：授权记录的存取（``grant`` / ``revoke`` / ``grant_matches``）
    与判定同属一个事实来源，组合形态下 ``PermissionManager`` 契约只暴露布尔 ``check``，
    取不到「显式授权命中了哪个动作」，而两轴求值需要把它并入内容轴的动作集合。
    """

    def requires_space_facts(self) -> bool:
        """空间级判据要成员表与归属登记，由鉴权点一次读取后传入。"""
        return True

    def check(
        self,
        actor: Scope,
        target: Scope,
        action: Action,
        context: PermissionContext | None = None,
    ) -> bool:
        """``PermissionManager`` 契约：判定结论的布尔折算。

        鉴权点若要拿到通过的轴（空间策略裁剪需要它），改调 :meth:`decide`。
        """
        return self.decide(actor, target, action, context).allowed

    def decide(
        self,
        actor: Scope,
        target: Scope,
        action: Action,
        context: PermissionContext | None = None,
    ) -> DecisionOutcome:
        """完整判定结论，含 ``rule`` 与通过的轴。

        迁移时本方法直接对应 ``Authorizer.authorize``：入参换成 ``AuthContext`` 与
        ``ResourceDescriptor``，返回值换成 ``AuthorizationDecision``，方法体不动。
        """
        entry = self._attr(context, ATTR_SPACE_ENTRY)
        rule = ENTRY_RULES.get(entry)
        if rule is None or rule.axis is SpaceAxis.ORG:
            # 未登记入口与组织级入口不落两轴求值：前者尚未纳入空间级判定，后者由管理面
            # 角色闸门终局裁决而组织级角色不属本特性范围。两者一律回落父类判据，行为与改造前
            # 一致，不因判定实现的装配而收紧。
            allowed = super().check(actor, target, action, context)
            return DecisionOutcome(allowed=allowed, rule="fallback_legacy_check")

        space_action = self._space_action(rule.action, context)
        author_principal = self._author_principal(context)
        principal_path = self._attr(context, ATTR_PRINCIPAL_PATH)
        facts = context.space_facts if context is not None else None
        covered = scope_covers(actor, target, context)
        granted = self._granted_actions(actor, target, space_action, context)

        axes = self._axes_for(rule.axis, self._attr(context, ATTR_SPACE_AXIS))
        outcome = DecisionOutcome(allowed=False, rule="no_axis_evaluated")
        for axis in axes:
            outcome = decide_axis(
                actor=actor,
                target=target,
                facts=facts,
                entry=entry,
                action=space_action,
                axis=axis,
                author_principal=author_principal,
                principal_path=principal_path,
                granted_actions=granted,
                scope_covered=covered,
                own_actions_apply=rule.own_actions_apply,
            )
            if outcome.allowed:
                return outcome
        return outcome

    @staticmethod
    def _axes_for(axis: SpaceAxis, requested: str) -> tuple[SpaceAxis, ...]:
        """本次要依次尝试哪几条轴。

        ``EITHER`` 的入口返回空间元数据、不含条目内容，内容轴成员与纯治理管理员都应
        看得到；判定仍是一次一轴，按「治理轴在前」依次尝试。次序不可颠倒：治理轴不查
        授权记录，且其结论要被空间策略裁剪复用。

        调用方可经属性通道指定只求某一条轴，用于鉴权点分两段判定的场景。
        """
        if requested == SpaceAxis.CONTENT.value:
            return (SpaceAxis.CONTENT,)
        if requested == SpaceAxis.GOVERNANCE.value:
            return (SpaceAxis.GOVERNANCE,)
        if axis is SpaceAxis.EITHER:
            return (SpaceAxis.GOVERNANCE, SpaceAxis.CONTENT)
        return (axis,)

    @staticmethod
    def _space_action(default: SpaceAction, context: PermissionContext | None) -> SpaceAction:
        """本次要求的动作。

        ``evolve`` 的去重与遗忘两种模式取 ``UPDATE``，由鉴权点经属性通道覆盖入口表的
        默认动作；两种模式都不放宽到「本人所写」——输入是一批条目，逐条判会使一次调用
        部分生效部分被拒。
        """
        if context is None:
            return default
        override = context.metadata.get(ATTR_SPACE_ACTION, "")
        try:
            return SpaceAction(override) if override else default
        except ValueError:
            return default

    @staticmethod
    def _attr(context: PermissionContext | None, key: str) -> str:
        return context.metadata.get(key, "") if context is not None else ""

    @staticmethod
    def _author_principal(context: PermissionContext | None) -> str | None:
        """条目作者主体；``None`` 表示目标不含条目信息。

        是否携带由键是否存在表达，不另设布尔字段——携带时该值恒非空。
        """
        if context is None:
            return None
        return context.metadata.get(AUTHOR_PRINCIPAL) or None

    def _granted_actions(
        self,
        actor: Scope,
        target: Scope,
        action: SpaceAction,
        context: PermissionContext | None,
    ) -> frozenset[SpaceAction]:
        """显式授权命中的动作集合。

        只探测本次要求的那个动作：两轴求值最终只判该动作是否落在集合内，逐个探测其余
        动作会在鉴权路径上产生与结论无关的存储访问。
        """
        grant_action = _GRANT_ACTIONS.get(action)
        if grant_action is None:
            return frozenset()
        if actor.org != target.org:
            return frozenset()
        if self.grant_matches(actor, target, grant_action, context):
            return frozenset({action})
        return frozenset()


@PermissionProducer.register("space_aware")
def _build(config):
    db_path = config.get("db_path", ":memory:")
    return SpaceAwarePermissionManager(str(db_path))
