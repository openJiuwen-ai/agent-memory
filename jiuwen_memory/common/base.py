"""共享能力插件基类：跨层复用的无状态计算能力。

这些能力不属于任何单独一层——构建层与检索层（以及接入、演进路径）都会
调用，因此抽到 ``src/common`` 下统一定义：

- **Tokenizer 分词**：构建层建全文倒排索引时分词；检索层对 query 做同样
  分词——两侧必须用同一分词器，否则倒排召回错位。
- **Chunker 切分**：构建层写入时把内容切成 chunk；重索引/演进路径按同一
  规则重切，保证切分结果可复现。
- **Embedder 向量化**：构建层对 chunk 向量化建向量索引；检索层对 query
  向量化做 ANN 召回——必须同一模型/维度才落在同一向量空间。
- **FeatureExtractor 特征抽取**：构建层富化记忆（关键词/实体/标签）；
  检索层抽取 query 特征做精确匹配与图召回。
- **LLM 大模型调用**（vLLM/OpenAI 兼容）：构建层用于信息提取/摘要/升华；
  检索层用于 query 改写/答案合成；自演进用于冲突消解——具体 prompt 由
  调用方负责，本接口保持通用。
- **Normalizer 模态规约**：接入层写入时把原模态规约为文本/结构投影；
  构建/演进的重建路径重跑同一规约器以重建投影（投影可复现的前提）。
- **Reranker 重排**：检索层融合召回后的精排；构建/演进写入流水线的
  相似去重与冲突消解同样需要对候选记忆按相关性排序。

插件只接收/返回普通值，不触及租户作用域的存储。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum


class PluginType(str, Enum):
    TOKENIZER = "tokenizer"
    CHUNKER = "chunker"
    EMBEDDER = "embedder"
    FEATURE_EXTRACTOR = "feature_extractor"
    LLM = "llm"
    ASR = "asr"
    NORMALIZER = "normalizer"
    RERANKER = "reranker"


class Plugin(ABC):
    """所有共享能力插件的自描述契约。"""

    @abstractmethod
    def plugin_type(self) -> PluginType:
        """返回本插件的能力类型。"""

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛 :class:`~common.errors.HealthCheckError`。"""
