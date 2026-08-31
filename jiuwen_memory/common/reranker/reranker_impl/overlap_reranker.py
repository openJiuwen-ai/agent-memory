# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~common.reranker.base.Reranker`——词重叠精排。

对每条候选文本，按其与 query 的分词重叠占比打分（顺序与输入一致）。真实实现用
交叉编码器等更强模型；这里用词重叠占位，足以演示「融合后精排」的重排序效果。
排序/截断由调用方完成。分词复用注入的 Tokenizer。
"""

from __future__ import annotations

from typing import List

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.reranker.base import Reranker, RerankerProducer
from jiuwen_memory.common.tokenizer import Tokenizer
from jiuwen_memory.common.tokenizer.base import TokenizerProducer


class OverlapReranker(Reranker):
    """query 与候选的分词重叠占比作相关性分。"""

    def __init__(self, tokenizer: Tokenizer) -> None:
        self._tokenizer = tokenizer

    def plugin_type(self) -> PluginType:
        return PluginType.RERANKER

    def health(self) -> None:
        return None

    def rerank(self, query: str, texts: List[str]) -> List[float]:
        q = set(self._tokenizer.tokenize(query))
        if not q:
            return [0.0 for _ in texts]
        scores: List[float] = []
        for text in texts:
            toks = self._tokenizer.tokenize(text)
            hits = sum(1 for t in toks if t in q)
            scores.append(hits / (len(toks) + 1))
        return scores


# -- 注册到 RerankerProducer（实现自注册，新增无需改 producer/build_kernel） ------ #



@RerankerProducer.register("overlap")
def _build(config):
    tokenizer = TokenizerProducer.dep(config, default="whitespace")
    return OverlapReranker(tokenizer)
