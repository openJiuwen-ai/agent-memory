# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Fuser — 多路融合 + 重排（架构 §7 ③）。

把各召回通道的候选合并去重、归一化打分并融合排序（如 RRF / 加权），
可选调用共享的 :class:`~common.reranker.Reranker` 做精排。
重排开关与融合策略按配置裁剪（端侧可关重排降时延）。
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import ScoredCandidate

from .base import RetrievalOperator
from .types import ParsedQuery, RecallChannel


def to_recall_channel(raw: str) -> RecallChannel:
    """把配置里的通道名（``RecallChannel`` 成员值或成员名，任意大小写）解析为枚举。

    解析顺序：按成员值精确匹配（``"vector"``）→ 回退按成员名匹配
    （``"VECTOR"`` / ``"Vector"`` 等大小写变体）→ 两者皆失败抛
    :class:`~common.errors.ValidationError`（携带合法成员清单），不再裸抛
    ``KeyError``。非字符串键（如 ``int``）同样归入非法输入，一并抛
    ``ValidationError`` 而非逸出 ``AttributeError``。各 ``channel_weights`` 配置
    消费方（weighted_rrf / score_max / BM25_scored 三类 Fuser）共用本 helper。

    :param raw: 通道名（配置值统一来自 YAML，约定为 ``str``；越界类型同样兜底）。
    :raises ValidationError: ``raw`` 既非成员值也非成员名，或不是字符串。
    """
    try:
        return RecallChannel(raw)
    except ValueError:
        pass
    except TypeError:
        raise ValidationError(f"recall channel must be a str, got {type(raw).__name__}") from None
    try:
        return RecallChannel[str(raw).upper()]
    except KeyError:
        allowed = ", ".join(item.value for item in RecallChannel)
        raise ValidationError(
            f"invalid recall channel {raw!r}, must be one of: {allowed}"
        ) from None


def normalize_channel_weights(
    weights: Mapping[RecallChannel | str, float | str],
) -> dict[RecallChannel, float]:
    """通道权重归一化的共享实现：枚举键直通、字符串键经 :func:`to_recall_channel` 解析。"""
    normalized: dict[RecallChannel, float] = {}
    for raw_channel, raw_weight in weights.items():
        if isinstance(raw_channel, RecallChannel):
            channel = raw_channel
        else:
            channel = to_recall_channel(raw_channel)
        normalized[channel] = float(raw_weight)
    return normalized


class FuserProducer(Factory):
    """Fuser 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``fuser_impl`` 下以 ``@FuserProducer.register("<名>")``
    自注册——注册发生在 import 实现模块时，由
    :func:`retrieval.bootstrap.register_operators` 统一触发。
    """

    TOP_NAME = "fuser"


class Fuser(RetrievalOperator):
    @abstractmethod
    def fuse(
        self, query: ParsedQuery, candidates: list[list[ScoredCandidate]]
    ) -> list[ScoredCandidate]:
        """融合多路候选（每路一个列表），返回统一排序后的 top 候选。"""
