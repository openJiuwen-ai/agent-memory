# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~common.embedder.base.Embedder`——确定性哈希词袋向量。

无外部模型：分词（复用注入的 Tokenizer → 与关键词通道同词表）后，把每个 token
用稳定哈希散到固定维度的桶里计数，再 L2 归一化。共享词多的文本向量夹角小、
余弦相似度高，足以驱动一条「语义近邻」召回路做 demo。构建侧与检索侧用同一实例
即落在同一向量空间。真实部署替换为真模型即可，维度对齐由 :meth:`dimension` 保证。
"""

from __future__ import annotations

import math
from typing import List

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder.base import Embedder, EmbedderProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.tokenizer import Tokenizer
from jiuwen_memory.common.tokenizer.base import TokenizerProducer

logger = get_logger(__name__)


def _bucket(token: str, dim: int) -> int:
    """稳定哈希（不受 PYTHONHASHSEED 影响），把 token 映射到 [0, dim)。"""
    h = 0
    for ch in token:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h % dim


class HashingEmbedder(Embedder):
    """哈希词袋 + L2 归一化的确定性向量化器。"""

    def __init__(self, tokenizer: Tokenizer, dim: int = 64) -> None:
        self._tokenizer = tokenizer
        self._dim = dim

    def plugin_type(self) -> PluginType:
        return PluginType.EMBEDDER

    def health(self) -> None:
        return None

    def dimension(self) -> int:
        return self._dim

    def embed(self, texts: List[str]) -> List[List[float]]:
        logger.info("HashingEmbedder: embedding %d texts (dim=%d)", len(texts), self._dim)
        out: List[List[float]] = []
        for text in texts:
            vec = [0.0] * self._dim
            for tok in self._tokenizer.tokenize(text):
                vec[_bucket(tok, self._dim)] += 1.0
            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0.0:
                vec = [v / norm for v in vec]
            out.append(vec)
        return out


# -- 注册到 EmbedderProducer（实现自注册，新增无需改 producer/build_kernel） ------ #



@EmbedderProducer.register("hashing")
def _build(config):
    # Tokenizer 经 TokenizerProducer 自取（缺省 whitespace），与索引/查询侧共享同一实例 → 同词表。
    tokenizer = TokenizerProducer.dep(config, default="whitespace")
    return HashingEmbedder(tokenizer, dim=config.get("embedder_dim", 64))
