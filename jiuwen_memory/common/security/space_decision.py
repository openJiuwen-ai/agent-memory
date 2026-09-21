"""空间感知判定的判据主体（F07「判定规则」）。

本模块是判定链中不访问存储的全部判据：组织边界、归属对比、归属主体档两级与两轴
求值。写成纯函数而不绑定任何宿主契约，是为了让判定宿主可替换——当前由安全层
``SpaceAwareAuthorizer`` 与控制层 ``space_aware`` 实现共用，换宿主时只改宿主、
判据不动（F07 决策 4）。

**结论带 rule 而非只返回布尔值**：带判据的结论对象要求放行与拒绝两侧都填 ``rule``。
主体直接返回布尔值则换宿主时每个分支都要补 ``rule``，因此这里一次把 ``rule`` 与拒绝
原因带出，由宿主折算成它自己的结论类型（F07 决策 4 的编码约束一）。

**步骤按目标次序编排**，不照抄控制层现有实现的次序：后者是「主体覆盖 → 组织边界」，
而正确次序要求组织边界在前，以使跨组织请求得到准确的拒绝原因（F07 决策 4 的编码约束
二）。本模块覆盖十步中的第 6、7、9、10 步；第 8 步主体覆盖与第 9 步委托复核的
结果均由宿主算出后传入（宿主回真源查询，本模块保持无存储判据的定位）；第 1、3、4、5
步在本特性内无判据来源，见 F07 决策 4「四步无判据来源」。
"""

from __future__ import annotations

from collections.abc import Mapping
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
# 定义落安全层是因为写入方（鉴权点，API 层）与读取方（判定宿主）分处两侧，
# 键名在任一侧定义都会形成跨层依赖。条目作者主体复用 principal.AUTHOR_PRINCIPAL——
# 它与条目 metadata 上的键同名同义，分设两个常量会在改名时漏改其一。
ATTR_SPACE_AXIS = "space_axis"
ATTR_SPACE_ENTRY = "space_entry"
ATTR_SPACE_ACTION = "space_action"
ATTR_PRINCIPAL_PATH = "principal_path"

# 空间事实经属性通道传递的投影键（F07「空间事实的两层投影」第二层）。判定宿主换成
# Authorizer 后，事实不再以 ``PermissionContext.space_facts`` 的形态进宿主，而是由
# 鉴权点折算成字符串属性随资源描述传入；键名与四个通道键同为稳定契约。
#
# 投影是**按调用方算好的布尔与档位**，不是原始记录：属性通道与调用方可写的
# metadata 同池，逐条传原始 owner/member 记录等于把成员表交给调用方伪造。鉴权点
# 写入前先剥掉调用方 metadata 里同名键（见 ``_apply_space_policy_context``）。
ATTR_SPACE_FACTS = "space_facts"
ATTR_SPACE_IS_INDIVIDUAL = "space_is_individual"
ATTR_SPACE_HAS_OWNERS = "space_has_owners"
ATTR_SPACE_OWNER_COVERED = "space_owner_covered"
ATTR_SPACE_OWNER_SAME_DIMS = "space_owner_same_dims"
ATTR_SPACE_USER_CONTENT_ROLE = "space_user_content_role"
ATTR_SPACE_USER_GOVERNANCE_ROLE = "space_user_governance_role"
ATTR_SPACE_AGENT_CONTENT_ROLE = "space_agent_content_role"
ATTR_SPACE_AGENT_GOVERNANCE_ROLE = "space_agent_governance_role"

# 鉴权点独占的属性通道键全集：写入侧持有者是 PEP，调用方 metadata 里的同名键一律
# 先剥掉再由 PEP 填入真值。
PEP_OWNED_ATTR_KEYS = (
    ATTR_SPACE_AXIS,
    ATTR_SPACE_ENTRY,
    ATTR_SPACE_ACTION,
    ATTR_PRINCIPAL_PATH,
    ATTR_SPACE_FACTS,
    ATTR_SPACE_IS_INDIVIDUAL,
    ATTR_SPACE_HAS_OWNERS,
    ATTR_SPACE_OWNER_COVERED,
    ATTR_SPACE_OWNER_SAME_DIMS,
    ATTR_SPACE_USER_CONTENT_ROLE,
    ATTR_SPACE_USER_GOVERNANCE_ROLE,
    ATTR_SPACE_AGENT_CONTENT_ROLE,
    ATTR_SPACE_AGENT_GOVERNANCE_ROLE,
)

# 主体维次序的合法取值与默认值。取自空间策略的 principal_path，经属性通道以字符串
# 传入——安全层不反向依赖控制层，因此不引用该层的枚举类型（不变量 3）。
_PATH_DIMS: dict[str, tuple[str, str]] = {
    "user_agent": ("user", "agent"),
    "agent_user": ("agent", "user"),
}
_DEFAULT_PATH = "user_agent"


class DenyReason(str, Enum):
    """拒绝原因枚举。

    拒绝原因的取值是审计与告警的匹配依据，属封闭集合。F07 原生指派两项；第 9 步
    代操作委托实装（P1-3）后补入两项委托取值——字符串值与授权层
    :class:`~common.security.types.DenyReason` 对齐，宿主按值折算，两侧审计按同一
    个 reason code 匹配。

    跨组织拒绝复用 ``NOT_COVERED``，由 ``rule`` 取 ``cross_org`` 在审计侧区分：F07 要求
    第 6 步排在第 8 步之前以得到准确的拒绝原因，但未指派专门的取值。这是一处已知的未定项，
    不臆造取值。
    """

    CONTEXT_MISMATCH = "context_mismatch"  # 空间级目标未携带空间事实
    NOT_COVERED = "not_covered"  # 无成员记录、无归属主体档、动作不在集合内
    DELEGATION_INVALID = "delegation_invalid"  # 委托不存在/已撤销/已过期/绑定不符
    DELEGATION_ACTION = "delegation_action"  # 动作不在委托 allowlist 内或不可委托


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


@dataclass(frozen=True)
class SpaceFactsProjection:
    """空间事实在单次判定视角下的投影：按调用方算好的布尔与两轴档位。

    成员档位按维取最具体记录后拆成两轴字段，``None`` 表示该维无记录——与
    ``most_specific`` 返回 ``None`` 同义，区别于档位为 ``NONE`` 的记录（两者在两轴
    求值里的处置不同，见 :func:`_axis_evaluation`）。
    """

    is_individual: bool
    has_owners: bool
    owner_covered: bool
    owner_same_dims: bool
    user_content_role: SpaceContentRole | None = None
    user_governance_role: SpaceGovernanceRole | None = None
    agent_content_role: SpaceContentRole | None = None
    agent_governance_role: SpaceGovernanceRole | None = None


def _projection_of(actor: Scope, facts: SpaceAuthorizationFacts) -> SpaceFactsProjection:
    """折算判定视角下的事实投影，供 ``decide`` 薄壳与属性通道共用。"""
    user_member = most_specific(facts.members, actor, dim="user") if actor.user else None
    agent_member = most_specific(facts.members, actor, dim="agent") if actor.agent else None
    return SpaceFactsProjection(
        is_individual=facts.is_individual,
        has_owners=bool(facts.owners),
        owner_covered=any(covers_owner(owner, actor) for owner in facts.owners),
        owner_same_dims=len(facts.owners) == 1 and same_dims(facts.owners[0], actor),
        user_content_role=user_member.content_role if user_member is not None else None,
        user_governance_role=user_member.governance_role if user_member is not None else None,
        agent_content_role=agent_member.content_role if agent_member is not None else None,
        agent_governance_role=(agent_member.governance_role if agent_member is not None else None),
    )


def project_facts(actor: Scope, facts: SpaceAuthorizationFacts) -> dict[str, str]:
    """把空间事实投影为属性通道的字符串键值（PEP 侧使用）。

    键缺省语义：成员档位键仅在调用方该主体维非空时写入，键缺即「该维不参与求值」；
    键在而值为空串表示该维无记录。布尔一律 ``true`` / ``false``。
    """
    projection = _projection_of(actor, facts)
    attrs = {
        ATTR_SPACE_FACTS: "1",
        ATTR_SPACE_IS_INDIVIDUAL: str(projection.is_individual).lower(),
        ATTR_SPACE_HAS_OWNERS: str(projection.has_owners).lower(),
        ATTR_SPACE_OWNER_COVERED: str(projection.owner_covered).lower(),
        ATTR_SPACE_OWNER_SAME_DIMS: str(projection.owner_same_dims).lower(),
    }
    if actor.user:
        attrs[ATTR_SPACE_USER_CONTENT_ROLE] = (
            projection.user_content_role.value if projection.user_content_role is not None else ""
        )
        attrs[ATTR_SPACE_USER_GOVERNANCE_ROLE] = (
            projection.user_governance_role.value
            if projection.user_governance_role is not None
            else ""
        )
    if actor.agent:
        attrs[ATTR_SPACE_AGENT_CONTENT_ROLE] = (
            projection.agent_content_role.value if projection.agent_content_role is not None else ""
        )
        attrs[ATTR_SPACE_AGENT_GOVERNANCE_ROLE] = (
            projection.agent_governance_role.value
            if projection.agent_governance_role is not None
            else ""
        )
    return attrs


def projection_from_attributes(
    attributes: Mapping[str, str],
) -> SpaceFactsProjection | None:
    """从属性通道还原投影；标记键缺失即「未取到事实」，返回 ``None``。

    通道键由鉴权点独占写入，这里的解析只做形状还原；取值不合法时向拒绝方向处置
    （布尔取假、档位按 ``NONE`` 记录），不抛——一个键值写坏不该把判定变成 500。
    """
    if attributes.get(ATTR_SPACE_FACTS, "") != "1":
        return None

    def _flag(key: str) -> bool:
        return attributes.get(key, "false") == "true"

    def _content(key: str) -> SpaceContentRole | None:
        return _role(SpaceContentRole, key, attributes)

    def _governance(key: str) -> SpaceGovernanceRole | None:
        return _role(SpaceGovernanceRole, key, attributes)

    return SpaceFactsProjection(
        is_individual=_flag(ATTR_SPACE_IS_INDIVIDUAL),
        has_owners=_flag(ATTR_SPACE_HAS_OWNERS),
        owner_covered=_flag(ATTR_SPACE_OWNER_COVERED),
        owner_same_dims=_flag(ATTR_SPACE_OWNER_SAME_DIMS),
        user_content_role=_content(ATTR_SPACE_USER_CONTENT_ROLE),
        user_governance_role=_governance(ATTR_SPACE_USER_GOVERNANCE_ROLE),
        agent_content_role=_content(ATTR_SPACE_AGENT_CONTENT_ROLE),
        agent_governance_role=_governance(ATTR_SPACE_AGENT_GOVERNANCE_ROLE),
    )


def _role(role_type, key: str, attributes: Mapping[str, str]):
    """还原单维档位：键缺即该维不参与，值为空串即无记录，非法值按 ``NONE``。"""
    if key not in attributes:
        return None
    raw = attributes[key]
    if raw == "":
        return None
    try:
        return role_type(raw)
    except ValueError:
        return role_type.NONE


def axes_for(axis: SpaceAxis, requested: str) -> tuple[SpaceAxis, ...]:
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


def resolve_space_action(default: SpaceAction, attributes: Mapping[str, str]) -> SpaceAction:
    """本次要求的动作，属性通道可覆盖入口表默认值。

    ``evolve`` 的去重与遗忘两种模式取 ``UPDATE``，由鉴权点经属性通道覆盖；两种模式
    都不放宽到「本人所写」——输入是一批条目，逐条判会使一次调用部分生效部分被拒。
    """
    override = attributes.get(ATTR_SPACE_ACTION, "")
    try:
        return SpaceAction(override) if override else default
    except ValueError:
        return default


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
    delegation: DecisionOutcome | None = None,
) -> DecisionOutcome:
    """求一条轴的判定结论（事实对象形态，旧宿主薄壳）。

    ``facts`` 直接取 :class:`SpaceAuthorizationFacts`；内部折算投影后走
    :func:`decide_projected`，两条入参形态共用同一份判据。

    :param facts: 空间授权事实的最小投影；``None`` 表示鉴权点未取到，按拒绝处理。
    """
    projection = None if facts is None else _projection_of(actor, facts)
    return decide_projected(
        actor=actor,
        target=target,
        projection=projection,
        entry=entry,
        action=action,
        axis=axis,
        author_principal=author_principal,
        principal_path=principal_path,
        granted_actions=granted_actions,
        scope_covered=scope_covered,
        own_actions_apply=own_actions_apply,
        delegation=delegation,
    )


def decide_projected(
    *,
    actor: Scope,
    target: Scope,
    projection: SpaceFactsProjection | None,
    entry: str,
    action: SpaceAction,
    axis: SpaceAxis,
    author_principal: str | None = None,
    principal_path: str = _DEFAULT_PATH,
    granted_actions: frozenset[SpaceAction] = frozenset(),
    scope_covered: bool = False,
    own_actions_apply: bool = True,
    delegation: DecisionOutcome | None = None,
) -> DecisionOutcome:
    """求一条轴的判定结论（属性通道投影形态）。

    :param actor: 调用方身份。会话维不参与比对。
    :param target: 目标 scope，取其 org 与 space 两维。
    :param projection: 空间事实经鉴权点折算的投影；``None`` 表示未取到，按拒绝处理。
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
    :param delegation: 第 9 步代操作委托的复核结论，由宿主按 ``delegation_id`` 回
        DelegationStore 真源求出后传入；``None`` 表示请求未声明委托，本步不参与。
        非 ``None`` 且本入口与本轴落在 :func:`on_behalf_paths_apply` 之内时**终局**
        ——与标准链一致，声明了委托就不再回落成员记录/Grant；落在其外则本步整体不参与。
    """
    if axis is SpaceAxis.ORG:
        # 组织级入口由管理面角色闸门终局裁决，不落两轴求值。组织级角色不属本特性范围，
        # 鉴权点不应把这类入口送进本函数。
        return _deny(DenyReason.CONTEXT_MISMATCH, "org_entry_not_evaluated_here")

    # 第 6 步 组织边界。排在主体覆盖之前，使跨组织请求得到准确的拒绝原因。
    if actor.org != target.org:
        return _deny(DenyReason.NOT_COVERED, "cross_org")

    if projection is None:
        # 空间级目标缺事实即拒绝，不回落为「判定实现自行读取」——回落会掩盖装配缺失，
        # 且使性能预算不可核算（不变量 2）。
        return _deny(DenyReason.CONTEXT_MISMATCH, "missing_space_facts")

    # 第 7 步 归属对比。只放行内容轴，且按「逐维相同」裁决的入口不走本步。
    if _owner_comparison_passes(actor, projection, entry, axis, author_principal):
        return _allow("owner_comparison", axis)

    # 第 8 步 主体覆盖。结果由宿主传入；保留该步是为了不改上游对非空间级资源的既有
    # 行为。条目真源 scope 归一为空间级后不带主体维，本步不命中。
    if scope_covered:
        return _allow("scope_covers", axis)

    # 第 9 步 代操作委托。适用范围与第 7 步同（见 on_behalf_paths_apply）：委托放行的
    # 是代理替人操作，治理轴与「逐维相同」入口不在其内——否则一条带 UPDATE/DELETE 的
    # 内容委托能改策略、删空间，一条带 READ 的能整空间导出，绕开归属主体档两级。
    # 不适用时本步整体不参与（等同请求未声明委托），继续走第 10 步。
    #
    # 适用且已声明时**终局**：命中即放行、失效即拒绝，均不落第 10 步——与标准链一致
    # （F05 §Delegation），失效委托不静默改判成员记录/Grant，审计里才看得出委托失效过。
    if delegation is not None and on_behalf_paths_apply(entry, axis):
        return _allow(delegation.rule, axis) if delegation.allowed else delegation

    # 第 10 步第一段 归属主体档。必须排在「主维无记录即拒绝」之前：预建的主空间恒不写
    # 成员记录，排在其后则该档整体不可达，症状是治理入口静默失效而条目读写照常。
    owner_entry_rule = _owner_entry_grade(projection, entry)
    if owner_entry_rule:
        return _allow(owner_entry_rule, axis)

    # 第 10 步第二段 两轴求值。
    return _axis_evaluation(
        actor=actor,
        projection=projection,
        action=action,
        axis=axis,
        author_principal=author_principal,
        principal_path=principal_path,
        granted_actions=granted_actions,
        own_actions_apply=own_actions_apply,
    )


def on_behalf_paths_apply(entry: str, axis: SpaceAxis) -> bool:
    """「代人操作」类判据是否适用于本入口与本轴（F07「第 7 步的两条排除」）。

    第 7 步归属对比与第 9 步代操作委托共用本判据。两步放行的是同一类情形——**不是
    主体本人、而是替他操作的代理**：第 7 步的代理身份来自 scope 覆盖（用户名下的
    代理），第 9 步来自委托记录（第三方 agent/service）。来源不同，能触达的范围必须
    相同，否则两条排除等于只对前一种代理生效。

    两条排除：

    - 只适用内容轴。治理动作转后续步骤，否则删空间、改策略经代理调用时在本步放行，
      归属主体档的两级随之失效。
    - 按「逐维相同」裁决的入口不适用。``export_space`` 取内容轴读动作、又不带条目
      信息，只有轴排除时本步对它无条件放行；整空间导出产出的是脱离后续判定的全量副本，
      须按归属主体档第一级裁决。

    公开（无前导下划线）而非私有：判定宿主也用它决定是否需要回委托真源查询——不适用
    的入口不该在鉴权路径上产生与结论无关的存储访问。两处用途共用一份定义，排除清单
    因此不会在宿主侧抄出第二份。
    """
    if axis is not SpaceAxis.CONTENT:
        return False
    return entry not in OWNER_ENTRY_SAME_DIMS


def _owner_comparison_passes(
    actor: Scope,
    projection: SpaceFactsProjection,
    entry: str,
    axis: SpaceAxis,
    author_principal: str | None,
) -> bool:
    """第 7 步：个体空间、调用方覆盖归属登记、作者标记比对通过。

    两条排除见 :func:`on_behalf_paths_apply`——与第 9 步代操作委托共用。
    """
    if not on_behalf_paths_apply(entry, axis):
        return False
    if not projection.is_individual or not projection.has_owners:
        return False
    if not projection.owner_covered:
        return False
    # 目标不含条目信息时跳过作者比对；携带时比对作者主体项。是否携带由取值是否为
    # None 表达，不另设布尔字段。
    return author_principal is None or author_match(actor, author_principal)


def _owner_entry_grade(projection: SpaceFactsProjection, entry: str) -> str:
    """第 10 步第一段：归属主体档两级，命中返回 rule，未命中返回空串。

    第一级另有项数判据：登记多于一项时一律不放行。多归属空间与预建主空间的事实形态
    相同（归属登记非空、成员表为空），唯一区别是登记项数，而两级清单本身不看项数。
    这八个入口作用于整个空间或其成员表，任一归属者执行即处置其他归属者的条目。
    第二级的四个入口只读空间元数据、不含条目内容，不受该限制。
    """
    if not projection.has_owners:
        return ""
    if entry in OWNER_ENTRY_SAME_DIMS:
        if projection.owner_same_dims:
            return "owner_entry_same_dims"
        return ""
    if entry in OWNER_ENTRY_COVERS and projection.owner_covered:
        return "owner_entry_covers"
    return ""


def _axis_evaluation(
    *,
    actor: Scope,
    projection: SpaceFactsProjection,
    action: SpaceAction,
    axis: SpaceAxis,
    author_principal: str | None,
    principal_path: str,
    granted_actions: frozenset[SpaceAction],
    own_actions_apply: bool,
) -> DecisionOutcome:
    """第 10 步第二段：按维取最具体记录、并入显式授权、两维取交、按轴判含。

    内部次序不可颠倒：先取最具体（已在投影里按维折算）、再与显式授权取并集，主维无
    记录的拒绝判定排在并入之后——提前则任何显式授权对非成员一律不生效。

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
        if dim == "user":
            content_role = projection.user_content_role
            governance_role = projection.user_governance_role
        else:
            content_role = projection.agent_content_role
            governance_role = projection.agent_governance_role
        actions: set[SpaceAction] = set()
        if axis is SpaceAxis.CONTENT:
            if content_role is not None:
                actions |= CONTENT_ACTIONS[content_role]
                if is_own:
                    actions |= CONTENT_ACTIONS_OWN[content_role]
            # 显式授权只在内容轴参与求值：治理权只由成员记录与归属主体档决定。该惰性
            # 同时是空间元数据入口「治理轴先判」的成本前提。
            actions |= granted_actions
            no_record = content_role is None
        else:
            if governance_role is not None:
                actions |= GOVERNANCE_ACTIONS[governance_role]
            no_record = governance_role is None
        if no_record and not actions:
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
    治理轴最高档）无判据来源，见 F07 决策 4「四步无判据来源」。
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


def exceeds_governance_ceiling(granted: SpaceGovernanceRole, ceiling: SpaceGovernanceRole) -> bool:
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
