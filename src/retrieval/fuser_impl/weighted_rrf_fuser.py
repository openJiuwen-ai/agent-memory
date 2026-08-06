"""Weighted RRF fuser — 带通道权重与融合证据的倒数排名融合。"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from common.type_def import ScoredCandidate
from retrieval.base import RetrievalOperatorType
from retrieval.fuser import Fuser, FuserProducer
from retrieval.types import ChannelEvidence, ParsedQuery, RecallChannel

from .layered_merge import merge_layered_channels


class WeightedRRFFuser(Fuser):
    """按通道权重加权的 RRF 融合。

    标准 RRF 对每条通道命中贡献 ``1 / (k + rank + 1)``；本实现额外乘以
    通道权重，并保留每个候选的融合证据，供后续披露和轨迹解释使用。
    """

    def __init__(
        self,
        k: int = 60,
        channel_weights: Mapping[RecallChannel | str, float | str] | None = None,
    ) -> None:
        self._k = k
        self._channel_weights = self._normalize_weights(channel_weights or {})

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.FUSER

    def health(self) -> None:
        return None

    def explain(self) -> dict[str, str]:
        sorted_weights = sorted(self._channel_weights.items(), key=lambda item: item[0].value)
        weight_parts = []
        for channel, weight in sorted_weights:
            weight_parts.append(f"{channel.value}={weight:g}")
        weights = ",".join(weight_parts)
        return {
            "strategy": "weighted_rrf",
            "rrf_k": str(self._k),
            "channel_weights": weights or "default=1",
        }

    def fuse(
        self, query: ParsedQuery, candidates: list[list[ScoredCandidate]]
    ) -> list[ScoredCandidate]:
        scores: dict[str, float] = {}
        channel: dict[str, RecallChannel] = {}
        evidence: dict[str, list[ChannelEvidence]] = {}
        representatives: dict[str, ScoredCandidate] = {}
        # 分层召回下同通道有多路（L2/L0/L1），先归并再计分（见 layered_merge）。
        for one_channel in merge_layered_channels(candidates):
            for rank, su in enumerate(one_channel):
                weight = self._channel_weights.get(su.channel, 1.0)
                contribution = weight / (self._k + rank + 1)
                scores[su.unit_id] = scores.get(su.unit_id, 0.0) + contribution
                channel.setdefault(su.unit_id, su.channel)
                representatives.setdefault(su.unit_id, su)
                evidence.setdefault(su.unit_id, []).append(
                    ChannelEvidence(
                        channel=su.channel,
                        rank=rank,
                        score=su.score,
                        weight=weight,
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

    @staticmethod
    def _normalize_weights(
        weights: Mapping[RecallChannel | str, float | str],
    ) -> dict[RecallChannel, float]:
        normalized: dict[RecallChannel, float] = {}
        for raw_channel, raw_weight in weights.items():
            if isinstance(raw_channel, RecallChannel):
                channel = raw_channel
            else:
                try:
                    channel = RecallChannel(raw_channel)
                except ValueError:
                    channel = RecallChannel[raw_channel.upper()]
            normalized[channel] = float(raw_weight)
        return normalized


# -- 注册到 FuserProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@FuserProducer.register("weighted_rrf")
def _build(config):
    return WeightedRRFFuser(
        k=config.get("fusion_rrf_k", 60),
        channel_weights=config.get("fusion_channel_weights", {}),
    )
