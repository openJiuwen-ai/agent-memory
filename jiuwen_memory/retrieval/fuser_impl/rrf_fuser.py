"""最小实现：:class:`~retrieval.fuser.Fuser`——RRF 倒数排名融合。

各路候选按 RRF（Reciprocal Rank Fusion）合并：同一 unit 在每路里按其名次 r 贡献
``1/(k+r+1)``，跨路累加得融合分，按融合分降序。RRF 与各路得分量纲无关，单路时
退化为按名次排序。重排（Reranker）按配置可选，最小实现不接入。
"""

from __future__ import annotations

from dataclasses import replace

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import ScoredCandidate
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.fuser import Fuser, FuserProducer
from jiuwen_memory.retrieval.types import ChannelEvidence, ParsedQuery, RecallChannel

from .layered_merge import merge_layered_channels


class RRFFuser(Fuser):
    """Reciprocal Rank Fusion：跨路按名次倒数累加。"""

    def __init__(self, k: int = 60) -> None:
        """初始化 RRFFuser。

        Args:
            k: 参数 k（int）。
        """
        self._k = k

    def operator_type(self) -> RetrievalOperatorType:
        """返回当前算子类型。

        Returns:
            返回 RetrievalOperatorType。
        """
        return RetrievalOperatorType.FUSER

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def explain(self) -> dict[str, str]:
        """执行 `explain` 操作。

        Returns:
            返回 dict[str, str]。
        """
        return {"strategy": "rrf", "rrf_k": str(self._k)}

    def fuse(
        self, query: ParsedQuery, candidates: list[list[ScoredCandidate]]
    ) -> list[ScoredCandidate]:
        """执行 `fuse` 操作。

        Args:
            query: 参数 query（ParsedQuery）。
            candidates: 参数 candidates（list[list[ScoredCandidate]]）。

        Returns:
            返回 list[ScoredCandidate]。

        Raises:
            KeyError: 执行失败时抛出。
        """
        scores: dict[str, float] = {}
        channel: dict[str, RecallChannel] = {}
        evidence: dict[str, list[ChannelEvidence]] = {}
        representatives: dict[str, ScoredCandidate] = {}
        # 分层召回下同通道有多路（L2/L0/L1），先归并再计分——否则同一 unit 的
        # 多层命中会被当作多路投票重复累加（见 layered_merge）。
        for one_channel in merge_layered_channels(candidates):
            for rank, su in enumerate(one_channel):
                contribution = 1.0 / (self._k + rank + 1)
                scores[su.unit_id] = scores.get(su.unit_id, 0.0) + contribution
                channel.setdefault(su.unit_id, su.channel)
                representatives.setdefault(su.unit_id, su)
                evidence.setdefault(su.unit_id, []).append(
                    ChannelEvidence(
                        channel=su.channel,
                        rank=rank,
                        score=su.score,
                        contribution=contribution,
                    )
                )
        fused: list[ScoredCandidate] = []
        for uid, score in scores.items():
            representative = representatives.get(uid)
            if representative is None:
                raise KeyError(uid)
            fused.append(
                replace(
                    representative,
                    score=score,
                    channel=channel.get(uid, RecallChannel.KEYWORD),
                    evidence=evidence.get(uid, []),
                )
            )
        fused.sort(key=lambda s: s.score, reverse=True)
        return fused


# -- 注册到 FuserProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@FuserProducer.register("rrf")
def _build(config):
    # RRF 常数 k 可经 params 覆盖（默认 60）。
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return RRFFuser(k=Factory.cfg_get(config, "k", 60))
