# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""检索层（D 层，架构 §7）算子基类。

检索链路五步：① scope/标签前置过滤 → ② 并行多路召回 → ③ 融合 + 重排
→ ④ 渐进式披露 → ⑤ 返回 + 检索轨迹。各环节拆成可插拔算子：

- :class:`~retrieval.query_parser.QueryParser` 查询理解（改写/分词/实体/向量化/时间解析）
- :class:`~retrieval.recaller.Recaller` 单路召回（每个启用的索引一路）
- :class:`~retrieval.fuser.Fuser` 多路融合 + 重排（RRF / Reranker）
- :class:`~retrieval.discloser.Discloser` 渐进式披露（L0 摘要 → L1 片段 → L2 全文）
- :class:`~retrieval.retriever.Retriever` 检索入口（编排整条链路，记录轨迹）

算子内部复用 ``src/common`` 共享插件——query 分词用 Tokenizer、向量化用
Embedder、实体抽取用 FeatureExtractor、改写用 LLM、精排用 Reranker，
与构建侧同一套实现才能保证同词表/同向量空间；读索引经注入的
``src/storage`` 各 Store，算子不依赖具体后端。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum


class RetrievalOperatorType(str, Enum):
    QUERY_PARSER = "query_parser"
    RECALLER = "recaller"
    FUSER = "fuser"
    DISCLOSER = "discloser"
    RETRIEVER = "retriever"


class RetrievalOperator(ABC):
    """所有检索层算子的自描述契约。"""

    @abstractmethod
    def operator_type(self) -> RetrievalOperatorType:
        """返回本算子的类型。"""

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。"""
