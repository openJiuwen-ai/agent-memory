# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""BM25 粗排算子 —— 在候选并集上补一路统一的词法信号，再做 CombMAX 融合。

注册名 ``BM25_scored_fusor`` 由总部指定，与配置里的 ``fuser.default.target`` 取值
逐字对应；类名/文件名沿用本目录的 ``Fuser`` 命名。

    combined(u) = max( max_c  weight_c × norm_c(u),  w_lex × norm_bm25(u) )

**为什么在 Elasticsearch 装配下仍然有用。** ES 的 keyword 通道分已是带全库统计的
Lucene ``BM25Similarity``，重估它只会更差——本算子**不重估**，只**补一路**：ES 只为
自己那一路的 top-k 打了分，**向量通道召回的候选从未被词法打过分**。融合时候选是各路
的并集，于是「有没有词法分」变成了「是哪一路召回的」的副产品，而不是相关性差异。
本算子在并集上统一算一遍 BM25，把这条轴补齐；各通道原分一律保留，只增不减。

与 ``score_max`` 同构（通道内 max 归一化 + 通道间取最大，F04 决策 1），词法分作为
额外一项参与取最大。不调用 ``Reranker``（AGENTS.md 本地约束 §7），不改召回。

IDF 与 avgdl 取自候选池而非全库——``Fuser`` 接口能拿到的最大范围。池宽为
``recall_max × 在场通道数``，比精排阶段的 ``rerank_max`` 更宽。这是已知近似：
本算子给出的是**池内相对**词法强度，不是全库可比的绝对分。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import replace
from typing import Mapping

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.tokenizer import Tokenizer
from jiuwen_memory.common.tokenizer.base import TokenizerProducer
from jiuwen_memory.common.type_def import ScoredCandidate
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.fuser import Fuser, FuserProducer
from jiuwen_memory.retrieval.types import ChannelEvidence, ParsedQuery, RecallChannel

from .layered_merge import merge_layered_channels

logger = get_logger(__name__)


def _bm25_scores(
    corpus: list[list[str]], query_tokens: list[str], k1: float, b: float
) -> list[float]:
    """Okapi BM25（Lucene ``BM25Similarity`` 公式），返回与 ``corpus`` 同序的得分::

        idf(t)   = log(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
        score(d) = Σ_t  idf(t) × freq(t,d) × (k1 + 1) / (freq(t,d) + k1 × norm(d))
        norm(d)  = 1 - b + b × dl(d) / avgdl

    与旧的词重叠表达式 ``hits / len(tokens)`` 的三点差异：IDF 加权（罕见词与停用词
    不再等权）、词频饱和（第 10 次命中不等价于第 1 次）、长度归一强度可调（``b``）。
    """
    n = len(corpus)
    if not n or not query_tokens:
        return [0.0] * n
    freqs = [Counter(doc) for doc in corpus]
    df: Counter[str] = Counter()
    for freq in freqs:
        df.update(freq.keys())
    total = sum(len(doc) for doc in corpus)
    # 空批 / 全空文档：avgdl 退化为 1.0，长度归一项恒为 1，不触发除零。
    avgdl = (total / n) if total else 1.0
    idf = {
        term: math.log(1.0 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
        for term in set(query_tokens)
    }
    scores: list[float] = []
    for doc, freq in zip(corpus, freqs):
        norm = k1 * (1.0 - b + b * len(doc) / avgdl)
        score = 0.0
        # query 内重复词按重复次数计入（与 Lucene 的 query 端行为一致）。
        for term in query_tokens:
            tf = freq.get(term, 0)
            if tf:
                score += idf[term] * tf * (k1 + 1.0) / (tf + norm)
        scores.append(score)
    return scores


class BM25ScoredFuser(Fuser):
    """候选并集上的 BM25 词法轴 + CombMAX 融合。"""

    def __init__(
        self,
        tokenizer: Tokenizer,
        k1: float = 1.2,
        b: float = 0.75,
        lexical_weight: float = 1.0,
        channel_weights: Mapping[RecallChannel | str, float | str] | None = None,
    ) -> None:
        self._tokenizer = tokenizer
        self._k1 = float(k1)
        self._b = float(b)
        # 词法轴相对各召回通道的话语权；0 = 关闭本算子的词法项，退化为 score_max。
        self._lexical_weight = float(lexical_weight)
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
        ordered = sorted(self._channel_weights.items(), key=lambda item: item[0].value)
        weights = ",".join(f"{ch.value}={w:g}" for ch, w in ordered)
        return {
            "strategy": "BM25_scored_fusor",
            "normalization": "channel_max",
            "k1": f"{self._k1:g}",
            "b": f"{self._b:g}",
            "lexical_weight": f"{self._lexical_weight:g}",
            "channel_weights": weights or "default=1",
        }

    # -- 内部 ------------------------------------------------------------- #

    def _query_tokens(self, query: ParsedQuery) -> list[str]:
        # 优先用 parser 已产出的 tokens：与构建侧同一 Tokenizer 实例（铁律 §2），
        # term 必然对得上；为空才退回自行分词。
        if query.tokens:
            return list(query.tokens)
        return self._tokenizer.tokenize(query.rewritten or query.raw)

    def _lexical_axis(
        self, query: ParsedQuery, pool: dict[str, ScoredCandidate]
    ) -> dict[str, float]:
        """并集上的 BM25 分，已按池内最高分归一化到 [0,1]；无文本可打分时返回空。"""
        if self._lexical_weight <= 0.0:
            return {}
        texts: dict[str, str] = {}
        for uid, candidate in pool.items():
            unit = getattr(candidate, "unit", None)
            # 未物化的 ScoredUnit 没有内容——该候选不参与词法轴，但不因此被打成 0 分。
            if unit is not None and unit.content:
                texts[uid] = unit.content
        q_tokens = self._query_tokens(query)
        if not texts or not q_tokens:
            logger.debug(
                "BM25ScoredFuser lexical axis skipped: scorable=%d q_tokens=%d",
                len(texts),
                len(q_tokens),
            )
            return {}
        order = list(texts)
        raw = _bm25_scores(
            [self._tokenizer.tokenize(texts[uid]) for uid in order],
            q_tokens,
            self._k1,
            self._b,
        )
        top = max(raw) if raw else 0.0
        if top <= 0.0:
            return {}
        return {uid: score / top for uid, score in zip(order, raw)}

    def fuse(
        self, query: ParsedQuery, candidates: list[list[ScoredCandidate]]
    ) -> list[ScoredCandidate]:
        # 分层召回下同通道占多路，先归并再归一化（与其余 Fuser 同一前处理）。
        merged = merge_layered_channels(candidates)
        if not merged:
            return []

        pool: dict[str, ScoredCandidate] = {}
        for one_channel in merged:
            for su in one_channel:
                pool.setdefault(su.unit_id, su)

        lexical = self._lexical_axis(query, pool)

        best: dict[str, float] = {}
        channel: dict[str, RecallChannel] = {}
        evidence: dict[str, list[ChannelEvidence]] = {}

        # ① 各召回通道：通道内 max 归一化后取加权分（score_max 口径，原分不动）。
        for one_channel in merged:
            ch = one_channel[0].channel
            weight = self._channel_weights.get(ch, 1.0)
            top = max(su.score for su in one_channel)
            for rank, su in enumerate(one_channel):
                # 非正的最高分（全零/负分通道）整路计 0，避免除零并保持
                # 「无有效信号即不贡献」的语义。
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
                if contribution > best.get(su.unit_id, -1.0):
                    best[su.unit_id] = contribution
                    channel[su.unit_id] = ch

        # ② 词法轴：并集上统一算一遍，作为额外一项参与取最大。只增不减。
        if lexical:
            ranked = sorted(lexical, key=lambda uid: lexical[uid], reverse=True)
            for rank, uid in enumerate(ranked):
                contribution = self._lexical_weight * lexical[uid]
                evidence.setdefault(uid, []).append(
                    ChannelEvidence(
                        channel=RecallChannel.KEYWORD,
                        rank=rank,
                        score=lexical[uid],
                        weight=self._lexical_weight,
                        contribution=contribution,
                    )
                )
                if contribution > best.get(uid, -1.0):
                    best[uid] = contribution
                    channel[uid] = RecallChannel.KEYWORD

        fused = [
            replace(
                pool[uid],
                score=score,
                channel=channel.get(uid, RecallChannel.KEYWORD),
                evidence=evidence.get(uid, []),
            )
            for uid, score in best.items()
        ]
        fused.sort(key=lambda su: su.score, reverse=True)
        return fused


# -- 注册到 FuserProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@FuserProducer.register("BM25_scored_fusor")
def _build(config):
    return BM25ScoredFuser(
        tokenizer=TokenizerProducer.dep(config, default="whitespace"),
        k1=float(config.get("bm25_k1", 1.2)),
        b=float(config.get("bm25_b", 0.75)),
        lexical_weight=float(config.get("bm25_lexical_weight", 1.0)),
        channel_weights=config.get("fusion_channel_weights", {}),
    )
