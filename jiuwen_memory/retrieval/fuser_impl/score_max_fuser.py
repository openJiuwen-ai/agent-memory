"""Score-max fuser — 通道内 max 归一化 + 通道间取最大值（CombMAX）。

    combined(u) = max_c  weight_c × ( score_c(u) / max_score_c )

两步，均在融合阶段完成，不改变召回：

1. **通道内 max 归一化**：每路把本次召回的最高分记作 1.0，其余按比例折算。消除量纲
   差异（BM25 无界、余弦约 [0,1]）而不引入任何阈值参数——"多少分算高"由本批数据
   自己给出，不依赖语料规模、BM25 实现或分析器。
2. **通道间取最大值**：候选取其在各通道归一化分中的最大值。

为什么取 max 而不是求和（CombSUM）：加法天然偏好多通道命中——两路在场时单通道候选
上限为 1/2，双通道上限为 1，一个语义极强但字面未命中的候选会被结构性折价。这是加法
的固有性质，换任何归一化都消不掉。RRF 是同一问题的极端形式（完全按命中路数计分）。
CombMAX 与 CombSUM/CombMNZ 同出 Fox & Shaw (TREC-2, 1994)；后者在"多系统检索同一
语料"的同质信号场景更优，而词法与语义是异质通道且候选集为并集，结论相反。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from jiuwen_memory.common.type_def import ScoredCandidate
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.fuser import Fuser, FuserProducer
from jiuwen_memory.retrieval.types import ChannelEvidence, ParsedQuery, RecallChannel

from .layered_merge import merge_layered_channels


class ScoreMaxFuser(Fuser):
    """通道内 max 归一化 + 通道间取最大值的融合器（零参数）。"""

    def __init__(
        self,
        channel_weights: Mapping[RecallChannel | str, float | str] | None = None,
    ) -> None:
        # 通道权重可选：默认全 1.0（不偏好任何通道）。用于人工压制/抬升某一路，
        # 不参与归一化——归一化基准始终是该通道自身的最高分。
        self._channel_weights = self._normalize_weights(channel_weights or {})

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

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.FUSER

    def health(self) -> None:
        return None

    def explain(self) -> dict[str, str]:
        ordered_weights = sorted(
            self._channel_weights.items(), key=lambda item: item[0].value
        )
        weight_parts: list[str] = []
        for channel, weight in ordered_weights:
            weight_parts.append(f"{channel.value}={weight:g}")
        weights = ",".join(weight_parts)
        return {
            "strategy": "score_max",
            "normalization": "channel_max",
            "channel_weights": weights or "default=1",
        }

    def fuse(
        self, query: ParsedQuery, candidates: list[list[ScoredCandidate]]
    ) -> list[ScoredCandidate]:
        # 分层召回下同通道有多路（L2/L0/L1），必须先归并再归一化——否则各层按各自
        # 最高分取基准，候选少的层会把弱命中抬到与主层最强候选同级（见 layered_merge）。
        merged = merge_layered_channels(candidates)

        best: dict[str, float] = {}
        channel: dict[str, RecallChannel] = {}
        evidence: dict[str, list[ChannelEvidence]] = {}
        representatives: dict[str, ScoredCandidate] = {}
        for one_channel in merged:
            ch = one_channel[0].channel
            weight = self._channel_weights.get(ch, 1.0)
            top = max(su.score for su in one_channel)
            for rank, su in enumerate(one_channel):
                # 通道内最高分记作 1.0；非正的最高分（全零/负分通道）整路计 0，
                # 避免除零并保持"无有效信号即不贡献"的语义。
                normalized = (su.score / top) if top > 0 else 0.0
                contribution = weight * normalized
                evidence.setdefault(su.unit_id, []).append(
                    ChannelEvidence(
                        channel=ch,
                        rank=rank,
                        score=su.score,
                        weight=weight,
                        contribution=contribution,
                    )
                )
                representatives.setdefault(su.unit_id, su)
                if contribution > best.get(su.unit_id, -1.0):
                    best[su.unit_id] = contribution
                    channel[su.unit_id] = ch

        fused: list[ScoredCandidate] = []
        for uid, score in best.items():
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
        fused.sort(key=lambda su: su.score, reverse=True)
        return fused


# -- 注册到 FuserProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@FuserProducer.register("score_max")
def _build(config):
    return ScoreMaxFuser(channel_weights=config.get("fusion_channel_weights", {}))
