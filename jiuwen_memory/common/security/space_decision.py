"""空间感知判定的判据主体（F07「判定规则」）。

本模块是判定链中不访问存储的全部判据：组织边界、归属对比、归属主体档两级与两轴
求值。写成纯函数而不绑定任何宿主契约，是为了让判定宿主可替换——当前由控制层
``PermissionManager`` 的 ``space_aware`` 实现调用，换宿主时只改宿主、判据不动
（F07 决策 4）。

**结论带 rule 而非只返回布尔值**：带判据的结论对象要求放行与拒绝两侧都填 ``rule``。
主体直接返回布尔值则换宿主时每个分支都要补 ``rule``，因此这里一次把 ``rule`` 与拒绝
原因带出，由宿主折算成它自己的结论类型（F07 决策 4 的编码约束一）。

**步骤按目标次序编排**，不照抄控制层现有实现的次序：后者是「主体覆盖 → 组织边界」，
而正确次序要求组织边界在前，以使跨组织请求得到准确的拒绝原因（F07 决策 4 的编码约束
二）。本模块覆盖十步中的第 6、7、10 步；第 8 步主体覆盖的结果由宿主算出后传入，
第 1、3、4、5、9 步在本特性内无判据来源，见 F07 决策 4「五步无判据来源」。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..type_def import Scope
from .principal import author_match, covers_owner, same_dims
from .space_roles import (
    CONTENT_ACTIONS,
    CONTENT_ACTIONS_OWN,
    CONTENT_RANK,
    GOVERNANCE_ACTIONS,
    GOVERNANCE_RANK,
    OWNER_ENTRY_COVERS,
    OWNER_ENTRY_SAME_DIMS,
    SpaceAction,
    SpaceAuthorizationFacts,
    SpaceAxis,
    SpaceContentRole,
    SpaceGovernanceRole,
    most_specific,
)

# 判定输入中经属性通道传递的四个键名（F07「空间事实的两层投影与传入通道」）。
# 它们落 PermissionContext.metadata，宿主换成别的资源描述对象时改传入通道即可；
# 定义落安全层是因为写入方（鉴权点，API 层）与读取方（判定宿主，控制层）分处两侧，
# 键名在任一侧定义都会形成跨层依赖。条目作者主体复用 principal.AUTHOR_PRINCIPAL——
# 它与条目 metadata 上的键同名同义，分设两个常量会在改名时漏改其一。
ATTR_SPACE_AXIS = "space_axis"
ATTR_SPACE_ENTRY = "space_entry"
ATTR_SPACE_ACTION = "space_action"
ATTR_PRINCIPAL_PATH = "principal_path"

# 主体维次序的合法取值与默认值。取自空间策略的 principal_path，经属性通道以字符串
# 传入——安全层不反向依赖控制层，因此不引用该层的枚举类型（不变量 3）。
_PATH_DIMS: dict[str, tuple[str, str]] = {
    "user_agent": ("user", "agent"),
    "agent_user": ("agent", "user"),
}
_DEFAULT_PATH = "user_agent"


class DenyReason(str, Enum):
    """拒绝原因枚举。

    拒绝原因的取值是审计与告警的匹配依据，属封闭集合，本规约不扩充它。这里只定义 F07
    明确指派的两项。

    跨组织拒绝复用 ``NOT_COVERED``，由 ``rule`` 取 ``cross_org`` 在审计侧区分：F07 要求
    第 6 步排在第 8 步之前以得到准确的拒绝原因，但未指派专门的取值。这是一处已知的未定项，
    不臆造取值。
    """

    CONTEXT_MISMATCH = "context_mismatch"  # 空间级目标未携带空间事实
    NOT_COVERED = "not_covered"  # 无成员记录、无归属主体档、动作不在集合内


@dataclass(frozen=True)
class DecisionOutcome:
    """一次判定的结论。

    ``axis`` 只在放行时有值，供鉴权点做空间策略裁剪——「两轴任一」的入口经治理轴
    通过才可读策略，只经内容轴通过则策略置空。
    """

    allowed: bool
    rule: str
    reason: DenyReason | None = None
    axis: SpaceAxis | None = None


def _allow(rule: str, axis: SpaceAxis) -> DecisionOutcome:
    return DecisionOutcome(allowed=True, rule=rule, axis=axis)


def _deny(reason: DenyReason, rule: str) -> DecisionOutcome:
    return DecisionOutcome(allowed=False, rule=rule, reason=reason)


def decide(
    *,
    actor: Scope,
    target: Scope,
    facts: SpaceAuthorizationFacts | None,
    entry: str,
    action: SpaceAction,
    axis: SpaceAxis,
    author_principal: str | None = None,
    principal_path: str = _DEFAULT_PATH,
    granted_actions: frozenset[SpaceAction] = frozenset(),
    scope_covered: bool = False,
    own_actions_apply: bool = True,
) -> DecisionOutcome:
    """求一条轴的判定结论。

    :param actor: 调用方身份。会话维不参与比对。
    :param target: 目标 scope，取其 org 与 space 两维。
    :param facts: 空间授权事实的最小投影；``None`` 表示鉴权点未取到，按拒绝处理。
    :param entry: 入口名，归属主体档按它查两级清单。
    :param action: 本次要求的动作。
    :param axis: 本次求哪条轴。``EITHER`` 不在此处展开——鉴权点按「治理轴在前」依次
        调用本函数两次，任一通过即放行。
    :param author_principal: 条目作者主体。``None`` 表示目标不含条目信息（如 search
        与 list），此时跳过作者比对。
    :param principal_path: 主体维次序，取 ``user_agent`` 或 ``agent_user``；缺失与
        非法值一律回落默认次序。
    :param granted_actions: 显式授权命中的动作集合，由宿主查授权记录后传入。只在内容
        轴参与求值——治理权只由成员记录与归属主体档决定。
    :param scope_covered: 第 8 步主体覆盖的结果，由宿主用覆盖判定算出。
    :param own_actions_apply: 「本人所写」附加集合是否适用于本入口。取假的入口其目标
        无法归属到单一作者（批量作用于整个空间），取值来自入口映射表。
    """
    if axis is SpaceAxis.ORG:
        # 组织级入口由管理面角色闸门终局裁决，不落两轴求值。组织级角色不属本特性范围，
        # 鉴权点不应把这类入口送进本函数。
        return _deny(DenyReason.CONTEXT_MISMATCH, "org_entry_not_evaluated_here")

    # 第 6 步 组织边界。排在主体覆盖之前，使跨组织请求得到准确的拒绝原因。
    if actor.org != target.org:
        return _deny(DenyReason.NOT_COVERED, "cross_org")

    if facts is None:
        # 空间级目标缺事实即拒绝，不回落为「判定实现自行读取」——回落会掩盖装配缺失，
        # 且使性能预算不可核算（不变量 2）。
        return _deny(DenyReason.CONTEXT_MISMATCH, "missing_space_facts")

    # 第 7 步 归属对比。只放行内容轴，且按「逐维相同」裁决的入口不走本步。
    if _owner_comparison_passes(actor, facts, entry, axis, author_principal):
        return _allow("owner_comparison", axis)

    # 第 8 步 主体覆盖。结果由宿主传入；保留该步是为了不改上游对非空间级资源的既有
    # 行为。条目真源 scope 归一为空间级后不带主体维，本步不命中。
    if scope_covered:
        return _allow("scope_covers", axis)

    # 第 10 步第一段 归属主体档。必须排在「主维无记录即拒绝」之前：预建的主空间恒不写
    # 成员记录，排在其后则该档整体不可达，症状是治理入口静默失效而条目读写照常。
    owner_entry_rule = _owner_entry_grade(actor, facts, entry)
    if owner_entry_rule:
        return _allow(owner_entry_rule, axis)

    # 第 10 步第二段 两轴求值。
    return _axis_evaluation(
        actor=actor,
        facts=facts,
        action=action,
        axis=axis,
        author_principal=author_principal,
        principal_path=principal_path,
        granted_actions=granted_actions,
        own_actions_apply=own_actions_apply,
    )


def _owner_comparison_passes(
    actor: Scope,
    facts: SpaceAuthorizationFacts,
    entry: str,
    axis: SpaceAxis,
    author_principal: str | None,
) -> bool:
    """第 7 步：个体空间、调用方覆盖归属登记、作者标记比对通过。

    两条排除（F07「第 7 步的两条排除」）：

    - 只放行内容轴。治理动作转后续步骤，否则删空间、改策略经代理调用时在本步放行，
      归属主体档的两级随之失效。
    - 按「逐维相同」裁决的入口不走本步。``export_space`` 取内容轴读动作、又不带条目
      信息，只有轴排除时本步对它无条件放行；整空间导出产出的是脱离后续判定的全量副本，
      须按归属主体档第一级裁决。
    """
    if axis is not SpaceAxis.CONTENT:
        return False
    if entry in OWNER_ENTRY_SAME_DIMS:
        return False
    if not facts.is_individual or not facts.owners:
        return False
    if not any(covers_owner(owner, actor) for owner in facts.owners):
        return False
    # 目标不含条目信息时跳过作者比对；携带时比对作者主体项。是否携带由取值是否为
    # None 表达，不另设布尔字段。
    return author_principal is None or author_match(actor, author_principal)


def _owner_entry_grade(actor: Scope, facts: SpaceAuthorizationFacts, entry: str) -> str:
    """第 10 步第一段：归属主体档两级，命中返回 rule，未命中返回空串。

    第一级另有项数判据：登记多于一项时一律不放行。多归属空间与预建主空间的事实形态
    相同（归属登记非空、成员表为空），唯一区别是登记项数，而两级清单本身不看项数。
    这八个入口作用于整个空间或其成员表，任一归属者执行即处置其他归属者的条目。
    第二级的四个入口只读空间元数据、不含条目内容，不受该限制。
    """
    if not facts.owners:
        return ""
    if entry in OWNER_ENTRY_SAME_DIMS:
        if len(facts.owners) == 1 and same_dims(facts.owners[0], actor):
            return "owner_entry_same_dims"
        return ""
    if entry in OWNER_ENTRY_COVERS and any(covers_owner(owner, actor) for owner in facts.owners):
        return "owner_entry_covers"
    return ""


def _axis_evaluation(
    *,
    actor: Scope,
    facts: SpaceAuthorizationFacts,
    action: SpaceAction,
    axis: SpaceAxis,
    author_principal: str | None,
    principal_path: str,
    granted_actions: frozenset[SpaceAction],
    own_actions_apply: bool,
) -> DecisionOutcome:
    """第 10 步第二段：按维取最具体记录、并入显式授权、两维取交、按轴判含。

    内部次序不可颠倒：先取最具体、再与显式授权取并集，主维无记录的拒绝判定排在并入
    之后——提前则任何显式授权对非成员一律不生效。

    主体维为空与该维无记录命中是两种情形：前者不参与求值，后者不构成约束、不参与收窄，
    两者都不进入取交。

    **作者标记未携带时「本人所写」附加集合按可能成立处置。** 条目级入口分两段鉴权，
    第一段的目标是空间、不带条目信息；此时按不成立处置会使 ``contributor`` 在第一段即
    被拒——它的 ``UPDATE`` / ``DELETE`` 只存在于该附加集合里，改自己写的条目这条路径
    整体不可达。最终边界由第二段保证：它带条目真源的作者标记，按实际比对。判据与第 7
    步一致（见 :func:`_owner_comparison_passes`），两处不分叉。

    该处置只对目标可归属到单一作者的入口成立。批量作用于整个空间的入口（演进与两个
    任务入口）由 ``own_actions_apply`` 取假关闭附加集合：它们没有第二段来收边界，
    按可能成立处置即等于按最宽的一条放行整批。
    """
    dims = _PATH_DIMS.get(principal_path, _PATH_DIMS[_DEFAULT_PATH])
    primary = dims[0]
    is_own = own_actions_apply and (
        author_principal is None or author_match(actor, author_principal)
    )

    participating: list[frozenset[SpaceAction]] = []
    for dim in dims:
        if not getattr(actor, dim):
            continue  # 该维为空，不参与求值
        member = most_specific(facts.members, actor, dim=dim)
        actions: set[SpaceAction] = set()
        if member is not None:
            if axis is SpaceAxis.CONTENT:
                actions |= CONTENT_ACTIONS[member.content_role]
                if is_own:
                    actions |= CONTENT_ACTIONS_OWN[member.content_role]
            else:
                actions |= GOVERNANCE_ACTIONS[member.governance_role]
        if axis is SpaceAxis.CONTENT:
            # 显式授权只在内容轴参与求值：治理权只由成员记录与归属主体档决定。该惰性
            # 同时是空间元数据入口「治理轴先判」的成本前提。
            actions |= granted_actions
        if member is None and not actions:
            if dim == primary:
                return _deny(DenyReason.NOT_COVERED, "not_a_member")
            continue  # 非主维无记录命中：不构成约束、不参与收窄
        participating.append(frozenset(actions))

    if not participating:
        # 两个主体维皆空的调用不应到达数据面入口，由 require_principal 在鉴权点拦截。
        return _deny(DenyReason.NOT_COVERED, "no_principal_dimension")

    effective = frozenset.intersection(*participating)
    if action in effective:
        return _allow(f"axis_{axis.value}", axis)
    return _deny(DenyReason.NOT_COVERED, f"axis_{axis.value}_action_not_granted")


def governance_grade(actor: Scope, facts: SpaceAuthorizationFacts) -> SpaceGovernanceRole:
    """调用方在该空间的治理档，即成员记录授予上界的基数（F07「改写防护与三处上界」）。

    两维各取最具体记录后取较低档，划分规则复用判定链的同一实现，不另写一份。

    个体空间的归属主体取最高档。该行不在规约的基数表内，但缺它则归属主体加不了第一个
    成员：成员表为空时两维都无记录、基数为空档，而首条成员记录的补写发生在空间管理器
    内部，鉴权点此刻看到的仍是空表。表现是判定放行、上界校验拒绝，个体空间永远无法转
    为共享空间。

    组织级与平台级角色不属本特性范围，基数表的前两行（``ROOT`` 全档、组织 ``ADMIN``
    治理轴最高档）无判据来源，见 F07 决策 4「五步无判据来源」。
    """
    if facts.is_individual and any(same_dims(owner, actor) for owner in facts.owners):
        return SpaceGovernanceRole.OWNER
    grades: list[SpaceGovernanceRole] = []
    for dim in ("user", "agent"):
        if not getattr(actor, dim):
            continue
        member = most_specific(facts.members, actor, dim=dim)
        if member is not None:
            grades.append(member.governance_role)
    if not grades:
        return SpaceGovernanceRole.NONE
    return min(grades, key=lambda role: GOVERNANCE_RANK[role])


def content_grade(actor: Scope, facts: SpaceAuthorizationFacts) -> frozenset[SpaceAction]:
    """调用方在该空间的内容轴有效动作集合，即显式授权授出上界的基数。

    两维各取最具体记录的内容集合后取交，不含「本人所写」附加集合——后者以「这一条是
    本人所写」为条件、逐条成立，授出去即失去条件。

    个体空间的归属主体取可编辑档的集合，理由与 :func:`governance_grade` 同源（成员表
    为空时两维都无记录），但比较函数不同：治理轴取「主体维逐维相同」，本函数取「覆盖
    即可」。该分叉与判定链第 7 步一致——归属对比只放行内容轴且按覆盖比对，因此用户经
    其名下代理调用时内容轴取到可编辑档的集合、治理轴取空档。两处若统一，要么代理拿到
    治理权，要么用户经代理写不了自己的空间。
    """
    if facts.is_individual and any(covers_owner(owner, actor) for owner in facts.owners):
        return CONTENT_ACTIONS[SpaceContentRole.EDITOR]
    sets: list[frozenset[SpaceAction]] = []
    for dim in ("user", "agent"):
        if not getattr(actor, dim):
            continue
        member = most_specific(facts.members, actor, dim=dim)
        if member is None:
            continue
        sets.append(CONTENT_ACTIONS[member.content_role])
    if not sets:
        return frozenset()
    return frozenset.intersection(*sets)


def exceeds_governance_ceiling(
    granted: SpaceGovernanceRole, ceiling: SpaceGovernanceRole
) -> bool:
    """目标治理档是否高于授予方自身。

    改前值同受该上界约束——这一条实现「不能给拥有者降级或移除」：管理员可增设管理员，
    不能设立拥有者，也不能动已有的拥有者。
    """
    return GOVERNANCE_RANK[granted] > GOVERNANCE_RANK[ceiling]


def raises_own_grade(
    actor: Scope,
    target_scope: Scope,
    facts: SpaceAuthorizationFacts,
    content_role: SpaceContentRole,
    governance_role: SpaceGovernanceRole,
) -> bool:
    """这次成员写入是否把调用方自己的档位改高（自提禁止，两轴）。

    只约束治理轴不够：治理管理员一次加成员即可把自己的内容档设为可编辑，从而取得空间
    内全部条目的读写权——「能管成员而看不到内容」那一档一次调用即失效。

    比对按主体维逐维相同，不按覆盖：用户经其代理调用时目标写的是代理维记录，与本人的
    记录是两条，不构成自提。
    """
    if not same_dims(target_scope, actor):
        return False
    current_content = SpaceContentRole.NONE
    current_governance = SpaceGovernanceRole.NONE
    for member in facts.members:
        if same_dims(member.scope, actor):
            current_content = member.content_role
            current_governance = member.governance_role
            break
    if CONTENT_RANK[content_role] > CONTENT_RANK[current_content]:
        return True
    return GOVERNANCE_RANK[governance_role] > GOVERNANCE_RANK[current_governance]
