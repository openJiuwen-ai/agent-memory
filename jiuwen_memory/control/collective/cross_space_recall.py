"""跨空间召回的扇出、取数摊配与合并（F07「多空间读写」第 3—5 步）。

一次跨空间检索是五步编排。前两步半读 ``identity``、是鉴权点（PEP）的职责，留 API 层；
后三步不读 ``identity``、不做任何裁决，落本模块：

| 步 | 内容 | 落点 | 判据 |
|---|---|---|---|
| 1 定候选空间 | 显式 ``spaces`` 或主体反查索引 | API | 反查按 ``identity`` |
| 2 逐空间判权与状态校验 | ``PermissionManager.decide`` | API | 循环体就是 PEP（S03 不变量 22） |
| 2.5 逐空间谓词 | 路由值回注 + 两族系统谓词 | API | 谓词按 ``identity`` 与空间事实生成 |
| 3 取数摊配 | :func:`~retrieval.cross_space.allocate_quota` | 本模块 | 纯算术，不访问存储 |
| 4 召回扇出 | 每空间一次 ``recall`` | 本模块 | 机械 I/O 编排，落点与谓词都已由上游定妥 |
| 5 合并 | :func:`~retrieval.cross_space.merge` | 本模块 | 纯计算 |

**为什么第 3—5 步可以下沉而第 2 步不可以。** 判据取 S02「不做业务编排逻辑」那条的原文：
「移出本层是否还能按 ``identity`` 裁决」。第 2 步的循环体就是判权本身，移出即把 PEP 分裂
为两处；第 3—5 步在候选集与谓词都已定妥之后执行，把 ``top_k`` 摊成各空间的取数上界、把
查询发出去、把结果轮转合并，全程没有一处需要知道调用方是谁。留在 API 层的代价是那层多出
一段与鉴权无关的取数编排——逐空间构造 ``RetrievalQuery`` 的循环本身就是取数编排。

**本模块不持有引擎，召回经 ``recall`` 回调传入**，与 :mod:`.write_targets` 收 ``can_write``
回调同一形态。控制层因此既不 import 引擎实现，也不 import 检索算子；它只 import
:mod:`~retrieval.cross_space` 的三个纯函数（无 Producer 注册、实现不可替换、不访问存储与
模型），依赖方向为 control → retrieval，与 ``engine.py`` / ``pipeline.py`` 既有的类型
依赖同向，检索层不反向依赖控制层，无环。

**扇出失败与判权剔除分两路返回。** 本模块只返回自己产生的那一路（``space_failures``），
不把它并进 ``merged.errors``。并进去之后，空间级扇出失败（``channel=space``）与检索层的
分通道错误（``VECTOR`` / ``KEYWORD`` 等，由 :func:`~retrieval.cross_space.merge` 逐空间
并入）混在同一个列表里，API 层要为「整个空间挂了」写审计就只能按 ``channel is SPACE``
过滤——那是把审计判据绑在本模块的 ``channel`` 编码上，本模块改一次编码即静默改掉上层的
审计范围。分两路之后 API 直接拿列表写审计，再自行决定并进返回值。

返回给调用方的错误项一项不少：API 层负责 ``merged.errors.extend(denied + space_failures)``，
判权剔除、扇出失败与各空间的分通道错误三类齐全。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Sequence

from jiuwen_memory.common.log import get_logger, metadata_for_log, scope_for_log
from jiuwen_memory.common.type_def import ChannelError, FilterClause, Scope, and_merge
from jiuwen_memory.retrieval.cross_space import allocate_quota, merge, space_error
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult

logger = get_logger(__name__)

# 每空间一次召回。由 API 层以闭包给出（通常是 ``MemoryEngine.recall``）——本模块不持有
# 引擎，也不知道召回背后是本地栈还是云侧。
RecallCallback = Callable[[Scope, RetrievalQuery], Awaitable[RetrievalResult]]


@dataclass(frozen=True)
class SpaceRecallTarget:
    """一个已判权空间的召回目标：落点 scope 加该空间专属的系统谓词。

    ``clauses`` 由 API 层在逐空间判权的同一轮里算出（授权路由值回注 + 两族系统谓词），
    逐空间各一份——各空间的授权可以来自不同的策略，共用一份即某个空间按另一个空间的
    授权取数。本模块不解释这些子句，只把它们与调用方表达式合成一个 AND 下推。
    """

    scope: Scope
    clauses: tuple[FilterClause, ...] = ()


async def recall_spaces(
    targets: Sequence[SpaceRecallTarget],
    query: RetrievalQuery,
    *,
    top_k: int,
    recall: RecallCallback,
) -> tuple[RetrievalResult, list[ChannelError]]:
    """第 3—5 步：摊配取数上界、扇出召回、轮转合并。

    :param targets: 已判权的空间及其专属谓词，顺序即合并优先级（重复内容保留靠前的那条）。
    :param query: 查询骨架，只差 ``top_k`` 与逐空间谓词。``text`` / ``as_of`` /
        ``disclosure`` / ``max_tokens`` / ``with_trajectory`` / ``extensions`` 与调用方
        表达式（``filters``）由 API 层装配好一次，本模块按空间复制并补齐两项。
    :param top_k: 调用方要的条数，同时是各空间取数上界的一个封顶。
    :param recall: 每空间一次召回的回调。
    :returns: ``(合并结果, 空间级扇出失败)``。后者不在 ``merged.errors`` 里，理由见模块文档。

    **实际并发度取决于引擎实现。** 本函数按并发写就，但 ``recall`` 在 ``CloudEngine`` 与
    ``InMemoryEngine`` 中都是不含 ``await`` 的 ``async def``——内部的 ``retriever.retrieve``
    是同步阻塞调用，``gather`` 因而顺序执行，总时延是空间数乘以单空间时延。这是引擎侧的
    既有形态，不由本特性引入；要拿到真并发需 ``asyncio.to_thread`` 包住每空间召回（前提是
    确认检索器与存储客户端多线程共用安全），或由引擎层提供真正异步的 ``recall``。两者都属
    引擎层改动，本函数在其落地之前不声称并发收益。

    **单个空间失败不使整次调用失败。** 跨空间检索的语义是「在能读的空间里找」，一个后端
    故障导致整次返回空，与「这些空间里没有内容」在调用方看来不可区分。失败空间产出一条
    ``ChannelError`` 进 ``space_failures``，其余空间照常返回。
    """
    if not targets:
        return RetrievalResult(), []

    priority = [target.scope.space for target in targets]
    quota = allocate_quota(priority, top_k)
    gathered = await asyncio.gather(
        *(_recall_one(target, query, quota, recall) for target in targets),
        return_exceptions=True,
    )

    results: list[tuple[str, RetrievalResult]] = []
    failures: list[ChannelError] = []
    for target, outcome in zip(targets, gathered):
        space = target.scope.space
        if isinstance(outcome, BaseException):
            failures.append(space_error(space, type(outcome).__name__, str(outcome)))
            continue
        results.append((space, outcome))

    merged = merge(results, top_k=top_k, priority=priority)
    # 各召回器只记本层 hits，收窄与归并之后剩下什么无处可查；返回条目的 id 与所属空间是
    # 判断「谁被收窄掉了」的唯一依据。
    #
    # id 取 getattr 而不是直接取属性：``RetrievedItem.unit_id`` 有默认值、正常路径不会缺，
    # 但融合器是可替换实现，回传非 ``RetrievedItem`` 的对象时直接取属性会在这一行抛
    # AttributeError，把整次跨空间检索带失败。日志不该有让主流程失败的可能。
    logger.info(
        "cross-space recall: spaces=%d per_space_counts=%s failed=%s returned=%d ids=%s",
        len(results),
        [len(result.items) for _, result in results],
        [error.source for error in failures],
        len(merged.items),
        [str(getattr(item, "unit_id", ""))[:8] for item in merged.items],
    )
    return merged, failures


async def _recall_one(
    target: SpaceRecallTarget,
    query: RetrievalQuery,
    quota: dict[str, int],
    recall: RecallCallback,
) -> RetrievalResult:
    """一个空间的召回：补齐取数上界与本空间谓词后调回调。

    ``extensions`` 逐空间取副本：它顺 parser 透传给自定义检索模块，共用一份时任何一个
    空间的检索模块就地改写都会波及其余空间。
    """
    rq = replace(
        query,
        top_k=quota.get(target.scope.space, 1),
        extensions=dict(query.extensions or {}),
    )
    if target.clauses:
        # 两族谓词与调用方表达式合成一个 AND 一次下推，在 top-k 截断之前生效——召回后
        # 二次过滤会让被筛掉的条目白占召回名额，最终返回条数少于 ``top_k``。
        rq.filters = and_merge(rq.filters, list(target.clauses))
    # 谓词在此合成后一次下推，各召回器只记过滤后的条数。缺这一行时「召回为零」无法区分是
    # 谓词筛空还是本来就召不到——多个召回器同时归零只可能来自共用的谓词，但具体是哪一条
    # 谓词无处可查。
    logger.info(
        "cross-space recall: scope=%s quota=%d clauses=%s",
        scope_for_log(target.scope),
        rq.top_k,
        metadata_for_log(
            [
                {
                    "field": clause.field,
                    "op": getattr(clause.op, "value", clause.op),
                    "value": clause.value,
                }
                for clause in target.clauses
            ]
        ),
    )
    return await recall(target.scope, rq)
