"""跨空间检索的取数上界与结果合并（F07「多空间读写」）。

一次跨空间调用把同一个查询下发给多个空间，各空间按上界取数，再由本模块合并成一份结果。
三个函数覆盖同一次调用的三个环节：:func:`allocate_quota` 给出各空间的取数上界，缺口由
:func:`merge` 的轮转在合并阶段回收，:func:`space_error` 编码「某个空间整体没进结果」。
分处两层即同一件事的几端跨层。

**扇出编排不在本模块**：谁去召回、并发怎么起、失败怎么收，落控制层的
:mod:`~control.collective.cross_space_recall`——它调本模块的三个纯函数，本模块不反向依赖它。

**落检索层而非 API 层**：S04「管什么」第 4 条「融合：多路候选合并去重、归一化打分」正是
本模块所做的事，只是通道由「同一空间的多路」换成「多个空间」。两者都不访问存储、不调
模型，与本层的算子并列而非算子——无 Producer 注册、实现不可替换，因此不进工厂。
"""

from __future__ import annotations

from typing import Sequence

from ..common.type_def import ChannelError, RecallChannel
from .types import RetrievalResult

# 跨空间检索一次调用的取数总量上界。各空间的取数上界为 ``min(top_k, CAP // 空间数)``，
# 用于封住 ``top_k`` 传得很大时 N 个空间同时全量取数的成本。
TOTAL_FETCH_CAP = 400


def space_error(space: str, error_type: str, message: str) -> ChannelError:
    """把「某个空间整体没进结果」记成结果对象上的结构化错误。

    ``RecallChannel.SPACE`` 不是召回通道，是编排层的失败标记位；剔除原因经 ``error_type``
    区分：无权是 ``PermissionDeniedError``，后端故障是实际异常类名。

    **两侧共用一个构造点**：判权剔除发生在 API 层（PEP），扇出失败发生在控制层的
    :mod:`~control.collective.cross_space_recall`，两处产出同一形态的错误项。各写一份即
    ``channel`` / ``source`` 的编码在两层各有一份，改一处漏一处的表现是调用方按 ``source``
    分不出是哪个空间，且不报错。

    落本模块而非 ``common/``：它编码的是跨空间检索这一件事的失败形态，与
    :func:`allocate_quota`、:func:`merge` 同属一次调用的三个环节。
    """
    return ChannelError(
        channel=RecallChannel.SPACE, source=space, error_type=error_type, message=message
    )


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

    上界取 ``top_k``，总取数由两道封顶：候选空间数上限（控制层的 ``SPACE_FANOUT_LIMIT``，
    由调用方在传入 ``spaces`` 之前施加）与 :data:`TOTAL_FETCH_CAP`；前者限空间数，后者
    应付 ``top_k`` 传得很大的调用。本函数只施加后者，不引用前者。

    各空间同一上界，不按空间加权。加权需要一个跨空间可比的先验（哪个空间更可能有答案），
    而本模块不访问存储也不调模型，取不到这样的先验；缺口回收由 :func:`merge` 的轮转承担。
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
