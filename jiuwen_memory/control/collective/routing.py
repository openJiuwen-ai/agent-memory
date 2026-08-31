"""结论直写路径的归属判定调用与判定结果归一（F07「多空间读写」）。

**本模块存在的唯一理由是分层边界。** 判定本身由构建层的 ``Router`` 承担，判定输入
（``RouteContext``，含已鉴权的候选空间集合）由 API 层的鉴权点构造。二者之间需要一次
调用，而 S02 规定 API 层不调用构建层算子——这次调用因此落在控制层：API 层把成品
``RouteContext`` 与 ``Router`` 实例传入，本模块完成调用与结果归一。

**为什么不并入 Evolver 的判定路径。** 构建层已有一处判定插入点（
``OrchestratingEvolver._route``，在抽取之后、分层标注与去重之前），但结论直写路径不调
演进算子，构建层对它没有插入点：写入的是一条已经成形的结论，输入是调用方原文而非抽取
产出的派生单元。两条路径共用 ``route_batch``，落盘不变量因此只有一份实现。

**为什么不接判权回调。** ``RouteContext.candidates`` 是 API 层判权后给出的成品集合，
本模块只在集内取结果，不向上索要判权函数，也不读 ``identity``——后者由 S02 不变量 2
禁止下沉。
"""

from __future__ import annotations

import uuid
from typing import Sequence

from jiuwen_memory.common.type_def import MemoryUnit, Segment
from jiuwen_memory.common.type_def.memory import MEMORY_CLASS_KEY
from jiuwen_memory.construction.router import (
    RouteContext,
    RouteDecision,
    Router,
    route_batch,
)


def route_many(
    router: Router | None, contents: Sequence[str], ctx: RouteContext
) -> list[RouteDecision]:
    """``infer=false`` 且 ``scope.space`` 为空时的判定入口，整批一次送判。

    整批一次而不是逐条：``Router`` 的契约是每批一次模型调用，逐条调用既放大时延（N 条即
    N 次串行调用），又使同批条目的判据不一致，模型实现内部按 ``_ROUTE_BATCH_SIZE`` 的
    分批也随之失效。

    与派生单元的处置有一处不同：未装配、判定失败或判为丢弃时一律取 fallback，不能像派生
    单元那样直接剔除——落盘的是调用方给的内容。

    两个落盘不变量在 :func:`~construction.router.route_batch` 内完成，与构建层同一实现。
    """
    if not contents:
        return []
    # 探针 id 逐条给唯一值：模型实现按 id 把结论对回条目，缺省的空串在多条一批时会让
    # 各条互相覆盖、整批取到同一个结论。探针只用于判定，id 不进条目 metadata
    # （见 ``collective.decision_metadata``），也不落盘。
    probes = [
        MemoryUnit(id=str(uuid.uuid4()), segments=[Segment(content=content)])
        for content in contents
    ]
    decisions = route_batch(router, probes, ctx)
    resolved: list[RouteDecision] = []
    for index in range(len(probes)):
        # 防御性分支：``route_batch`` 保证返回与输入等长，短于输入时补齐的这条同样要带
        # reason——留空即它在审计里与「按判定原样落点」不可区分。
        decision = (
            decisions[index]
            if index < len(decisions)
            else RouteDecision(scope=ctx.fallback, reason="route_batch returned a short list")
        )
        if decision.discarded:
            decision = RouteDecision(
                scope=ctx.fallback,
                tags=dict(decision.tags),
                memory_class=next((item.name for item in ctx.classes if item.fallback), ""),
                reason="direct-write decision cannot discard caller-supplied content",
            )
        resolved.append(decision)
    return resolved


def decision_metadata(decision: RouteDecision) -> dict[str, str]:
    """判定结果落进条目 metadata 的那部分：全部判定标签键加类别记录键。

    与 :func:`route_many` 同处：两者都只对 ``RouteDecision`` 做归一，不含判据。探针 id
    只用于判定，不进条目 metadata、也不落盘。
    """
    metadata = dict(decision.tags)
    if decision.memory_class:
        metadata[MEMORY_CLASS_KEY] = decision.memory_class
    return metadata
