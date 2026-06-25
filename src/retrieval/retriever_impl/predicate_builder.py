"""PredicateBuilder — 把检索策略翻译成结构化前置过滤谓词（架构 §7 ①）。

由 lifecycle×as_of / 事件时间窗 / include_archived 生成一组 :class:`FilterClause`，
Retriever 在召回前并入 ``ParsedQuery.scalar_filters`` 下推到各 Store——使「先排除
什么」在索引级生效（减少召回浪费），而非只靠点读后过滤。

谓词形态与字段名遵循契约 spec（lifecycle 枚举值原文、时间为 epoch 毫秒 UTC）。
**与点读后 :func:`~retrieval.retriever_impl.unit_reader.passes` 并存=纵深防御**：
索引未写齐 lifecycle/temporal 或异步滞后时，后置过滤仍兜底正确性。
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from common.type_def import FilterClause, FilterOp
from common.type_def.memory import LifecycleState

# 当前态查询允许的 lifecycle；时间点回溯时排除的 lifecycle。
_ACTIVE_ONLY = [LifecycleState.ACTIVE.value]
_ACTIVE_ARCHIVED = [LifecycleState.ACTIVE.value, LifecycleState.ARCHIVED.value]


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def build_system_filters(
    as_of: Optional[datetime],
    time_from: Optional[datetime],
    time_to: Optional[datetime],
    include_archived: bool = False,
) -> List[FilterClause]:
    """生成系统前置谓词（与用户 filters 一同 AND 下推）。"""
    out: List[FilterClause] = []

    # lifecycle × as_of
    if as_of is None:
        allowed = _ACTIVE_ARCHIVED if include_archived else _ACTIVE_ONLY
        out.append(FilterClause("lifecycle", FilterOp.IN, allowed))
        # 当前态：valid-time 未失效（t_invalid 为空哨兵或 > now 由后端按索引语义判定）
    else:
        out.append(FilterClause("lifecycle", FilterOp.NE, LifecycleState.FORGOTTEN.value))
        t = _epoch_ms(as_of)
        out.append(FilterClause("t_valid", FilterOp.LTE, t))
        out.append(FilterClause("t_invalid", FilterOp.GT, t))

    # 事件时间窗（event-time，来自 query 文本解析）
    if time_from is not None:
        out.append(FilterClause("t_event", FilterOp.GTE, _epoch_ms(time_from)))
    if time_to is not None:
        out.append(FilterClause("t_event", FilterOp.LTE, _epoch_ms(time_to)))

    return out
