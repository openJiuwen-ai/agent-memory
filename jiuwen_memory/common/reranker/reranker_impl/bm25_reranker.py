"""最小实现：:class:`~common.reranker.base.Reranker`——Okapi BM25 精排。

对每条候选文本，按其与 query 的 BM25 相关性打分（顺序与输入一致）。真实实现用
交叉编码器等更强模型；这里用 BM25 占位，足以演示「融合后精排」的重排序效果。
排序/截断由调用方完成。分词复用注入的 Tokenizer。

IDF 取自候选批，而非全库
------------------------
``Reranker.rerank`` 只拿到 ``(query, texts)``，够不到库级统计量。故此处的 df 是
「这批候选里有多少条含该词」。对 shortlist 重排来说这正是要问的量：候选已被召回
阶段筛过一轮，此刻有区分力的是**批内**罕见词；一个批内条条都有的词，纵使全库罕见，
在这一步也不携带排序信息。

得分归一化到 [0, 1]
-------------------
BM25 原始分无上界，而 ``apply_threshold`` 把精排分当作**已校准**、按绝对刻度与
``min_score`` 比较。返回原始分会悄悄改变该阈值对既有配置的含义，故按批内最大值
归一。归一化不影响排序。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import List

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.reranker.base import Reranker, RerankerProducer
from jiuwen_memory.common.tokenizer import Tokenizer
from jiuwen_memory.common.tokenizer.base import TokenizerProducer



class BM25Reranker(Reranker):
    """Okapi BM25 精排：批内计分，归一化到 [0, 1]。"""

    def __init__(self, tokenizer: Tokenizer, *, k1: float = 1.5, b: float = 0.75) -> None:
        if k1 < 0:
            raise ValueError(f"k1 must be >= 0, got {k1}")
        if not 0.0 <= b <= 1.0:
            raise ValueError(f"b must be in [0, 1], got {b}")
        self._tokenizer = tokenizer
        self._k1 = k1
        self._b = b

    def plugin_type(self) -> PluginType:
        return PluginType.RERANKER

    def health(self) -> None:
        return None

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        q_terms = set(self._tokenizer.tokenize(query))
        if not q_terms or not texts:
            return [0.0 for _ in texts]

        tokenised = [self._tokenizer.tokenize(text) for text in texts]
        lengths = [len(tokens) for tokens in tokenised]
        non_empty = [length for length in lengths if length]
        if not non_empty:
            return [0.0 for _ in texts]
        avgdl = sum(non_empty) / len(non_empty)
        n = len(non_empty)

        df: dict[str, int] = defaultdict(int)
        for tokens in tokenised:
            for term in q_terms.intersection(tokens):
                df[term] += 1

        raw: list[float] = []
        for tokens, dl in zip(tokenised, lengths):
            tf: dict[str, int] = defaultdict(int)
            for token in tokens:
                if token in q_terms:
                    tf[token] += 1
            score = 0.0
            for term, freq in tf.items():
                # 与 InMemoryFulltextStore 同一口径（Lucene 的 IDF，log 内 +1 保证非负）。
                idf = math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
                denom = freq + self._k1 * (1.0 - self._b + self._b * dl / avgdl)
                score += idf * (freq * (self._k1 + 1.0)) / denom
            raw.append(score)

        top = max(raw)
        if top <= 0:
            return [0.0 for _ in texts]
        return [score / top for score in raw]


# -- 注册到 RerankerProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@RerankerProducer.register("bm25")
def _build(config):
    tokenizer = TokenizerProducer.dep(config, default="whitespace")
    # k1 / b 为 BM25 标准缺省，一般无需调整。
    return BM25Reranker(
        tokenizer,
        k1=float(config.get("reranker_bm25_k1", 1.5)),
        b=float(config.get("reranker_bm25_b", 0.75)),
    )