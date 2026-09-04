# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""PredicateBuilder — 把检索策略翻译成结构化前置过滤谓词（架构 §7 ①）。

由 lifecycle×as_of / 事件时间窗 / include_archived 生成一组 :data:`FilterExpr`，
Retriever 在召回前并入 ``ParsedQuery.scalar_filters`` 下推到各 Store——使「先排除
什么」在索引级生效（减少召回浪费），而非只靠点读后过滤。

谓词形态与字段名遵循契约 spec（lifecycle 枚举值原文、时间为 epoch 毫秒 UTC）。
**与点读后 :func:`~retrieval.retriever_impl.unit_reader.passes` 并存=纵深防御**：
索引未写齐 lifecycle/temporal 或异步滞后时，后置过滤仍兜底正确性。

事件窗下推以 ``OR(AND(GTE from, LT to), EQ T_EVENT_UNKNOWN)`` 表达——真源
``t_event is None`` 的派生 unit 在索引里落哨兵 ``T_EVENT_UNKNOWN=0``，谓词的
``EQ 0`` 分支使其不被窗下推清空（未知时间 ≠ 窗外，F07 净化的刻意取舍）。
"""

from __future__ import annotations

from datetime import datetime

from jiuwen_memory.common.type_def import (
    T_EVENT_UNKNOWN,
    FilterClause,
    FilterExpr,
    FilterGroup,
    FilterLogic,
    FilterOp,
)
from jiuwen_memory.common.type_def.memory import LifecycleState

# 当前态查询允许的 lifecycle；时间点回溯时排除的 lifecycle。
_ACTIVE_ONLY = [LifecycleState.ACTIVE.value]
_ACTIVE_ARCHIVED = [LifecycleState.ACTIVE.value, LifecycleState.ARCHIVED.value]


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def build_system_filters(
    as_of: datetime | None,
    time_from: datetime | None,
    time_to: datetime | None,
    include_archived: bool = False,
) -> list[FilterExpr]:
    """生成系统前置谓词（与用户 filters 一同 AND 下推）。"""
    out: list[FilterExpr] = []

    # lifecycle × as_of
    if as_of is None:
        allowed = _ACTIVE_ARCHIVED if include_archived else _ACTIVE_ONLY
        out.append(FilterClause("lifecycle", FilterOp.IN, allowed))
        # 当前态暂只前置 lifecycle；当前时刻的 valid-time 由 UnitReader 复核。
        # 因而设置未来 t_valid / 已过 t_invalid 的 ACTIVE 记录仍可能占用召回预算，
        # 这是当前态 top-k 完整性的已知限制。
    else:
        out.append(FilterClause("lifecycle", FilterOp.NE, LifecycleState.FORGOTTEN.value))
        t = _epoch_ms(as_of)
        out.append(FilterClause("t_valid", FilterOp.LTE, t))
        # 依赖索引投影的哨兵约定：真源 t_invalid 为空（永久有效）时索引里落
        # ``T_INVALID_OPEN``，故本谓词对开放区间同样成立。若哪天索引改回"空则不写"，
        # 这一行会把活跃记忆整批排他——两处必须同改（见 common.type_def.T_INVALID_OPEN）。
        out.append(FilterClause("t_invalid", FilterOp.GT, t))

    # 事件时间窗（event-time，来自 query 文本解析）。
    # 半开区间 [time_from, time_to)，与 in_event_window 后置复核同边界；
    # 用 LTE 会把 t_event == time_to 的记忆召回后再被复核砍掉，白占 top-k 预算。
    # OR 的 EQ 0 分支放行「未知事件时间」unit（真源 t_event=None → 索引哨兵 0），
    # 使含时间词 query 不再对无日期派生系统性空召回（见 T_EVENT_UNKNOWN）。
    if time_from is not None or time_to is not None:
        window_children: list[FilterExpr] = []
        if time_from is not None:
            window_children.append(
                FilterClause("t_event", FilterOp.GTE, _epoch_ms(time_from))
            )
        if time_to is not None:
            window_children.append(FilterClause("t_event", FilterOp.LT, _epoch_ms(time_to)))
        out.append(
            FilterGroup(
                FilterLogic.OR,
                [
                    FilterGroup(FilterLogic.AND, window_children),
                    FilterClause("t_event", FilterOp.EQ, T_EVENT_UNKNOWN),
                ],
            )
        )

    return out
