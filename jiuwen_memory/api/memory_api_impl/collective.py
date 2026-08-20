"""多空间读写的纯函数集（S09「多空间读写」）。

写入侧的候选空间集合、检索侧的两族谓词与配额分配、跨空间合并——都是不访问存储的机械
计算。集中在本模块而不是散进 :class:`~api.memory_api_impl.local_memory_api.LocalMemoryAPI`
的方法里，是为了可脱离装配单测：这些计算的失效方向多为放行或静默收窄，而两者在集成用例
里都不表现为报错。

本模块不做鉴权判定，只接收判权结果：判权要读空间事实、要走判定链，属鉴权点的职责。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security import principal
from jiuwen_memory.common.security.space_roles import SpaceAuthorizationFacts
from jiuwen_memory.common.type_def import FilterClause, FilterOp, Scope
from jiuwen_memory.common.type_def.memory import MEMORY_CLASS_KEY
from jiuwen_memory.construction.router import (
    KERNEL_COORD_KEYS,
    NarrowDim,
    RouteContext,
    RouteDecision,
    Router,
    SpaceNaming,
    route_batch,
)
from jiuwen_memory.control.types import SpaceFacts
from jiuwen_memory.retrieval import RetrievalResult

logger = get_logger(__name__)

# 写入候选空间数上限，与检索侧并发空间数上限同值。
SPACE_FANOUT_LIMIT = 8

# 跨空间检索一次调用的取数总量上界。各空间的取数上界为 ``min(top_k, CAP // 空间数)``，
# 用于封住 ``top_k`` 传得很大时 N 个空间同时全量取数的成本。
TOTAL_FETCH_CAP = 400


@dataclass(frozen=True)
class WriteTargets:
    """一次写入的候选空间集合与兜底落点。

    ``truncated`` 记因达到上限而未参与判权的空间数：截断不静默，调用处据此记 WARNING。
    """

    candidates: tuple[Scope, ...] = ()
    fallback: Scope = field(default_factory=Scope)
    truncated: int = 0


# ====================================================================== #
# 事实投影与坐标
# ====================================================================== #


def authorization_facts(facts: SpaceFacts | None) -> SpaceAuthorizationFacts | None:
    """控制层的空间事实折算为判定用的最小投影（S09「空间事实的两层投影」）。

    一次纯计算、不访问存储。空间策略、生命周期状态与成员记录的时间戳不进投影——判定不看
    它们，状态校验由鉴权点在授权通过之后另行执行。

    成员记录的存量单轴角色已在空间管理器读盘时解析成两轴，本函数直取两个字段。
    """
    if facts is None:
        return None
    from jiuwen_memory.common.security.space_roles import SpaceMemberFact

    owners = tuple(facts.info.owners) if facts.info is not None else ()
    members = tuple(
        SpaceMemberFact(
            scope=member.scope,
            content_role=member.content_role,
            governance_role=member.governance_role,
        )
        for member in facts.members
    )
    return SpaceAuthorizationFacts(owners=owners, members=members)


def reject_kernel_coords(coords: Mapping[str, str] | None) -> None:
    """入口拒绝调用方给内核三项坐标赋值。

    这三项以调用方身份为准，赋了值也不生效——静默丢弃即调用方以为自己指定了归属，实际
    被忽略，而落点与检索结果都会与预期不符且没有任何提示。判定表的加载期第 12 条已经禁止
    ``coord_entities`` 声明这三项，配置层禁止而调用层静默，两处口径不一致。

    只在 API 入口调一次；:func:`kernel_coords` 保持幂等的覆盖语义，供内部反复折算。
    """
    clash = sorted(set(coords or {}) & set(KERNEL_COORD_KEYS))
    if clash:
        raise ValidationError(
            f"coords 不得给内核坐标赋值：{clash}——这三项取自调用方身份，由内核填入"
        )


def kernel_coords(coords: Mapping[str, str] | None, identity: Scope) -> dict[str, str]:
    """装配判定上下文之前先以身份覆盖内核三项，全链路只有一份坐标。

    不这样做时同一批取值有两个来源：检索侧折算时用身份覆盖同名键，而写入侧的落点计算读的
    是上下文里的坐标，两处口径不同，后者的强度随调用方传入的坐标变化——坐标里的 user 与
    session 传空值即约束项减少，方向为放行。

    覆盖而非合并：内核三项以身份为准，调用方声明的同名取值直接丢弃。
    """
    merged = {str(key): str(value) for key, value in (coords or {}).items() if key}
    merged["user"] = identity.user
    merged["agent"] = identity.agent
    merged["session"] = identity.session
    return {key: value for key, value in merged.items() if value}


# ====================================================================== #
# 写入侧：候选空间集合与单条判定
# ====================================================================== #


def plan_write_targets(
    org: str,
    coords: Mapping[str, str],
    naming: SpaceNaming,
    *,
    can_write: Callable[[Scope], bool],
    limit: int = SPACE_FANOUT_LIMIT,
) -> WriteTargets:
    """算出本次写入的候选空间集合，全程机械计算。

    候选是两个集合的交：归属坐标按类别的空间名模板渲染出的相关空间，与调用方有写权的空间。
    渲染只作粗筛，权限由逐空间判权裁决。

    fallback 空间不在候选集内即整体拒绝写入——判不准时无处可落，此时静默落到别处等于把
    兜底落点交给判定实现决定。

    组织空间不进写入候选：它的归属维不是坐标键，模板渲染不出（加载期第 10 条校验要求模板
    必须引用该类别的 owner，而 org 不在坐标键集合内）。
    """
    fallback_space = naming.fallback_space(coords)
    rendered = naming.spaces(coords)

    ordered: list[str] = []
    if fallback_space:
        ordered.append(fallback_space)
    for space in rendered.values():
        if space not in ordered:
            ordered.append(space)

    allowed: list[Scope] = []
    truncated = 0
    for index, space in enumerate(ordered):
        if len(allowed) >= limit:
            # 上限已满，其余的不再判权：判权要读空间事实并走判定链，是本函数唯一的外部
            # 调用，全部判完再截断即多付这部分开销。截断因此按渲染顺序而非按写权结果，
            # fallback 排在首位、不会被截掉。
            truncated = len(ordered) - index
            logger.warning(
                "collective.plan_write_targets: 候选空间达到上限 %d，其余 %d 个未参与判权",
                limit,
                truncated,
            )
            break
        scope = Scope(org=org, space=space)
        if can_write(scope):
            allowed.append(scope)

    fallback = next((scope for scope in allowed if scope.space == fallback_space), None)
    if fallback is None:
        # fallback 空间由调用方自己的身份渲染而来，指出它的状态不构成资源枚举侧信道——
        # 调用方本来就知道自己是谁。其余候选空间不给同等提示：那会让任意主体可以逐个
        # 试探组织内有哪些空间存在。
        raise PermissionDeniedError(
            f"fallback space {org}/{fallback_space!r} is not writable: nowhere to fall back to. "
            f"该空间可能尚未注册——空间须先经 create_space 创建才能写入，"
            f"用户主空间由开通服务在用户开通时预建"
        )
    return WriteTargets(candidates=tuple(allowed), fallback=fallback, truncated=truncated)


def route_many(
    router: Router | None, contents: Sequence[str], ctx: RouteContext
) -> list[RouteDecision]:
    """``infer=false`` 且省略 ``scope`` 时的判定入口，整批一次送判。

    该路径不调演进算子，构建层没有插入点；而它确实需要判定——接入方的结论直写入口走的正是
    这条路径，写入的是一条已经成形的结论。不判定的后果是项目级事实经此入口永远进不了协作
    空间。

    整批一次而不是逐条：:class:`~construction.router.Router` 的契约是每批一次模型调用，逐条
    调用既放大时延（N 条即 N 次串行调用），又使同批条目的判据不一致，模型实现内部按
    ``_ROUTE_BATCH_SIZE`` 的分批也随之失效。

    与派生单元的处置有一处不同：未装配、判定失败或判为丢弃时一律取 fallback，不能像派生
    单元那样直接剔除——落盘的是调用方给的内容。

    两个落盘不变量在 :func:`~construction.router.route_batch` 内完成，与构建层同一实现。
    """
    from jiuwen_memory.common.type_def import MemoryUnit, Segment

    if not contents:
        return []
    # 探针 id 逐条给唯一值：模型实现按 id 把结论对回条目，缺省的空串在多条一批时会让
    # 各条互相覆盖、整批取到同一个结论。探针只用于判定，id 不进条目 metadata
    # （见 :func:`decision_metadata`），也不落盘。
    probes = [
        MemoryUnit(id=str(uuid.uuid4()), segments=[Segment(content=content)])
        for content in contents
    ]
    decisions = route_batch(router, probes, ctx)
    resolved: list[RouteDecision] = []
    for index in range(len(probes)):
        decision = decisions[index] if index < len(decisions) else RouteDecision(scope=ctx.fallback)
        if decision.discarded:
            decision = RouteDecision(
                scope=ctx.fallback,
                tags=dict(decision.tags),
                memory_class=next((item.name for item in ctx.classes if item.fallback), ""),
                reason="direct-write decision cannot discard caller-supplied content",
            )
        resolved.append(decision)
    return resolved


def route_single(router: Router | None, content: str, ctx: RouteContext) -> RouteDecision:
    """单条写入的判定入口；语义见 :func:`route_many`。"""
    return route_many(router, [content], ctx)[0]


def decision_metadata(decision: RouteDecision) -> dict[str, str]:
    """判定结果落进条目 metadata 的那部分：全部判定标签键加类别记录键。"""
    metadata = dict(decision.tags)
    if decision.memory_class:
        metadata[MEMORY_CLASS_KEY] = decision.memory_class
    return metadata


# ====================================================================== #
# 检索侧：两族谓词、配额与合并
# ====================================================================== #


def narrow_from_coords(
    coords: Mapping[str, str],
    dims: Iterable[NarrowDim],
    identity: Scope,
) -> dict[str, str]:
    """把归属坐标折算成收窄维取值：``标签键 -> 取值``，缺项不生成。

    坐标已由 :func:`kernel_coords` 以身份覆盖过内核三项，本函数不再覆盖一次——两处各覆盖
    一次即两份口径，改一处不改另一处时检索与写入对同一次调用得出不同的收窄条件。
    """
    resolved = kernel_coords(coords, identity)
    narrow: dict[str, str] = {}
    for dim in dims:
        value = str(resolved.get(dim.entity, "") or "").strip()
        if value:
            narrow[dim.tag_key] = value
    return narrow


def individual_space_predicates(
    facts: SpaceAuthorizationFacts | None, actor: Scope
) -> list[FilterClause]:
    """第一族：个体空间的系统谓词，条件由内核自算，接口上没有让调用方干预的口子。

    仅个体空间生效（成员表为空）。协作空间的可见范围由两轴角色裁决，按作者收窄会使其失去
    协作意义。

    | 调用形态 | 追加的谓词 |
    |---|---|
    | 代理自主运行 | ``author_principal == "agent:<id>"`` |
    | 用户本人直接调用，或经其名下代理调用 | 不追加，该空间内全部条目可见 |

    多归属空间（回填产物）另有一条：恒追加「作者主体等于调用方」，不看调用方形态——缺它
    则回填窗口内两个归属者互相召回得到对方的条目，且不报错。
    """
    if facts is None or not facts.is_individual:
        return []
    try:
        author_principal, _ = principal.derive_author(actor)
    except Exception:  # noqa: BLE001 —— 主体两维皆空的调用在形态校验处已拒绝
        # 谓词生成不是判定，抛错会把一个已经通过鉴权的调用变成失败。
        return []
    if len(facts.owners) > 1 or author_principal.startswith("agent:"):
        return [author_clause(author_principal)]
    return []


def author_clause(author_principal: str) -> FilterClause:
    return FilterClause(
        f"system_metadata.{principal.AUTHOR_PRINCIPAL}", FilterOp.EQ, author_principal
    )


def narrow_predicates(narrow: Mapping[str, str]) -> list[FilterClause]:
    """第二族：收窄维谓词，每个有取值的维生成 ``system_metadata.<tag_key> IN ["", <value>]``。

    取值为空串的条目一并命中，因此「该维不适用」与「判为否」的条目不会被收窄掉。坐标缺项
    不生成对应谓词，表现为该维不收窄——失效方向是放宽，不是越权。
    """
    clauses: list[FilterClause] = []
    for key in sorted(narrow):
        value = str(narrow[key] or "").strip()
        if value:
            clauses.append(FilterClause(f"system_metadata.{key}", FilterOp.IN, ["", value]))
    return clauses


def system_predicates(
    facts: SpaceAuthorizationFacts | None,
    actor: Scope,
    narrow: Mapping[str, str] | None = None,
) -> list[FilterClause]:
    """两族谓词合起来。调用方表达式与它们合成一个 AND 一次下推，在 top-k 截断之前生效。

    召回后二次过滤会让被筛掉的条目白占召回名额，最终返回条数少于 ``top_k``。
    """
    return individual_space_predicates(facts, actor) + narrow_predicates(narrow or {})


def allocate_quota(spaces: Sequence[str], top_k: int) -> dict[str, int]:
    """给出每个空间的取数上界，不是把 ``top_k`` 切成定额。

    定额分配的隐含前提是「每个空间都有至少 quota 条相关内容」，而空间按归属切分，规模天然
    不均——用户主空间可能有数千条，新建的协作空间只有几条。前提不成立时缺口有两个来源，
    两者在本特性里都是常态：

    | 缺口来源 | 表现 |
    |---|---|
    | 空间条目少于定额 | 该空间交不满，缺口不回流给有余量的空间 |
    | 跨空间重复内容 | 重复的那条在源空间已占定额，合并去重后名额空置 |

    两者的失效形态都是静默少返回：调用方拿不到 ``top_k`` 条，也没有任何提示。因此改取
    上界语义——各空间按上界取数，缺口由 :func:`merge` 的轮转在合并阶段回收。

    上界取 ``top_k``，总取数由候选空间上限（:data:`SPACE_FANOUT_LIMIT`）与
    :data:`TOTAL_FETCH_CAP` 两道封顶：前者限空间数，后者应付 ``top_k`` 传得很大的调用。

    各空间同一上界，不按空间加权。加权需要一个跨空间可比的先验（哪个空间更可能有答案），
    而本层不访问存储也不调模型，取不到这样的先验；缺口回收由 :func:`merge` 的轮转承担。
    """
    if not spaces or top_k <= 0:
        return {}
    per_space_cap = max(1, min(top_k, TOTAL_FETCH_CAP // len(spaces)))
    return {space: per_space_cap for space in spaces}


def merge(
    results: Sequence[tuple[str, RetrievalResult]],
    *,
    top_k: int,
    priority: Sequence[str] = (),
) -> RetrievalResult:
    """跨空间合并：各空间轮流出一条，按内容去重，凑够 ``top_k`` 即止。

    **本函数不做跨空间的相关性排序，这是一处已知局限，不是尚未实现。** 各空间结果项上的
    ``score`` 是相对量，跨空间不可比，两族融合器的不可比成因不同而结论一致：

    | 融合器 | 分数构造 | 为何跨空间不可比 |
    |---|---|---|
    | ``rrf`` / ``weighted_rrf``（默认 ``rrf``） | ``Σ 1/(k + rank + 1)``，纯名次函数 |
      只有 3 条内容的空间里排第 1 的那条，与数千条里排第 1 的那条得分相同 |
    | ``score_max`` | 通道内按本次召回最高分归一 | 每个空间的最高分都被归一到同一基准 |

    因此本函数在「多样性」与「相关性」之间显式选择多样性：轮转保证每个有内容的空间都有
    代表，但不保证排在前面的条目比排在后面的更相关——某空间的第 1 条会排在另一空间的第 2
    条之前，即使后者实际更相关。跨空间检索的目的是覆盖多个空间，该取舍与之一致。

    要按相关性跨空间排序，需要一个跨空间可比的绝对量：只有纯向量单通道的部署可直接用原始
    余弦分（同一模型、同一查询向量）；混合检索的部署中全文通道的 idf 由各自索引的文档频率
    算出、同样不可比，唯一干净的路径是对合并集做一次统一 rerank。那是独立工程——本函数是
    不访问存储、不调模型的纯函数，rerank 放进来会破坏该定位。

    轮转的两处行为使配额缺口自动回收：某空间的队列取空后本轮跳过它，其未用完的名额流给
    仍有内容的空间；取到重复内容时从同一队列继续向下取，重复消耗队列但不消耗 ``top_k``
    名额。重复内容保留优先级最高空间的那条，优先级由 ``priority`` 给出（未列出的空间排在
    其后）。

    ``errors`` 与 ``trajectory`` 逐空间合并保留：某个空间的通道失败不应在跨空间调用里消失。
    """
    order = {space: index for index, space in enumerate(priority)}
    ranked = sorted(results, key=lambda item: order.get(item[0], len(order)))

    merged = RetrievalResult()
    for _space, result in ranked:
        merged.errors.extend(result.errors)
        merged.trajectory.extend(result.trajectory)

    limit = max(0, top_k)
    pools = [list(result.items) for _space, result in ranked]
    seen: set[str] = set()
    while len(merged.items) < limit and any(pools):
        progressed = False
        for pool in pools:
            if len(merged.items) >= limit:
                break
            while pool:
                item = pool.pop(0)
                key = item.content.strip() or item.unit_id
                if key in seen:
                    continue
                seen.add(key)
                merged.items.append(item)
                progressed = True
                break
        if not progressed:
            # 全部队列的剩余项都是重复内容，再轮一圈也取不出新条目。
            break
    return merged
