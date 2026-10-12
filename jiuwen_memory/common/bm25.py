# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared Okapi BM25 scoring for tokenized corpora."""

from __future__ import annotations

import math
from collections import Counter


def bm25_scores(
    corpus: list[list[str]], query_tokens: list[str], k1: float = 1.2, b: float = 0.75
) -> list[float]:
    """Return Lucene-style Okapi BM25 scores in corpus order.

    The implementation follows ``BM25Similarity``::

        idf(t)   = log(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
        score(d) = sum_t idf(t) * freq(t,d) * (k1 + 1) /
                   (freq(t,d) + k1 * (1 - b + b * dl(d) / avgdl))
    """
    n = len(corpus)
    if not n or not query_tokens:
        return [0.0] * n

    freqs = [Counter(doc) for doc in corpus]
    df: Counter[str] = Counter()
    for freq in freqs:
        df.update(freq.keys())

    total = sum(len(doc) for doc in corpus)
    # An all-empty corpus would otherwise make avgdl zero. Keep the length norm at 1.
    avgdl = (total / n) if total else 1.0
    idf = {
        term: math.log(1.0 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
        for term in set(query_tokens)
    }

    scores: list[float] = []
    for doc, freq in zip(corpus, freqs):
        norm = k1 * (1.0 - b + b * len(doc) / avgdl)
        score = 0.0
        # Repeated query terms count once per occurrence, matching Lucene query behavior.
        for term in query_tokens:
            tf = freq.get(term, 0)
            if tf:
                score += idf[term] * tf * (k1 + 1.0) / (tf + norm)
        scores.append(score)
    return scores
