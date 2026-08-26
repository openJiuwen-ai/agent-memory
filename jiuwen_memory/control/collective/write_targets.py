"""写入候选空间集合的计算（F07「多空间读写」）。

候选是两个集合的交：归属坐标按类别的空间名模板渲染出的相关空间，与调用方有写权的空间。
渲染只作粗筛，权限由逐空间判权裁决。

**本模块不判权，也不持有判定器。** 判权要读空间事实、要走判定链，属鉴权点的职责；本模块
只经 ``can_write`` 回调取判权结果。回调由 API 层以闭包给出，``identity`` 被闭包捕获、不
出现在本模块的任何签名里，S02 不变量 2 因此不受影响。

**本模块也不抛权限异常。** 兜底落点不在候选集内时返回 ``fallback=None``，由鉴权点抛出。
控制层只出集合运算结果，权限异常的语义留在 PEP。

本模块决定的是**判权范围**——截断规则决定哪些候选空间根本不被送去判权。该裁剪可落本层，
前提是未判即不进候选、失效方向为拒绝而非放行。
"""

from __future__ import annotations

from typing import Callable, Mapping

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.construction.router import SpaceNaming

from ..types import WriteTargets

logger = get_logger(__name__)

# 写入候选空间数上限，与检索侧一次调用参与的空间数上限同值。
SPACE_FANOUT_LIMIT = 8


def plan_write_targets(
    org: str,
    coords: Mapping[str, str],
    naming: SpaceNaming,
    *,
    can_write: Callable[[Scope, bool], bool],
    limit: int = SPACE_FANOUT_LIMIT,
) -> WriteTargets:
    """算出本次写入的候选空间集合，全程机械计算。

    ``can_write`` 的第二个入参表示「除写权外是否还要求空间状态可写」，对 fallback 之外的
    候选取真。fallback 不加这项要求：它是唯一落点，状态不可写时把它排除只会把原因换成
    「无处可落」的权限拒绝，而调用方要看到的是「该空间已冻结或已归档」——两者的处置不同，
    区分性由错误类型承载。冻结态的 fallback 因此照常入选，由写入路径抛出状态错误。

    返回值的 ``fallback`` 为 ``None`` 即兜底落点不在候选集内，调用方据此整体拒绝写入。

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
    for index, space in enumerate(ordered):
        if index >= limit:
            # 上限已满，其余的不再判权：判权要读空间事实并走判定链，是本函数唯一的外部
            # 调用，全部判完再截断即多付这部分开销。截断因此按渲染顺序而非按写权结果，
            # fallback 排在首位、不会被截掉。截断不静默，记 WARNING。
            logger.warning(
                "plan_write_targets: 候选空间达到上限 %d，其余 %d 个未参与判权",
                limit,
                len(ordered) - index,
            )
            break
        scope = Scope(org=org, space=space)
        if can_write(scope, space != fallback_space):
            allowed.append(scope)

    fallback = next((scope for scope in allowed if scope.space == fallback_space), None)
    return WriteTargets(candidates=tuple(allowed), fallback=fallback)
