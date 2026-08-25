"""最小实现：:class:`~common.feature_extractor.base.FeatureExtractor`。

用注入的 Tokenizer 分词，去重得关键词；把较长的拉丁词当作 ``TERM`` 实体（占位，
真实实现用 NER）；不产稠密向量（向量由 Embedder 单独产）。供构建层富化记忆/
备图索引、检索层抽 query 特征共用。
"""

from __future__ import annotations

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.feature_extractor.base import FeatureExtractor, FeatureExtractorProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.tokenizer import Tokenizer
from jiuwen_memory.common.tokenizer.base import TokenizerProducer
from jiuwen_memory.common.type_def import Entity, FeatureSet

logger = get_logger(__name__)


class KeywordFeatureExtractor(FeatureExtractor):
    """分词关键词 + 拉丁长词实体的轻量特征抽取。"""

    def __init__(self, tokenizer: Tokenizer) -> None:
        """初始化 KeywordFeatureExtractor。

        Args:
            tokenizer: 参数 tokenizer（Tokenizer）。
        """
        self._tokenizer = tokenizer

    def plugin_type(self) -> PluginType:
        """返回当前插件类型。

        Returns:
            返回 PluginType。
        """
        return PluginType.FEATURE_EXTRACTOR

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def extract(self, text: str) -> FeatureSet:
        """执行 `extract` 操作。

        Args:
            text: 参数 text（str）。

        Returns:
            返回 FeatureSet。
        """
        tokens = self._tokenizer.tokenize(text)
        keywords = list(dict.fromkeys(tokens))  # 去重保序
        entities = [
            Entity(text=t, type="TERM", score=1.0)
            for t in keywords
            if t.isascii() and len(t) >= 3
        ]
        logger.info("KeywordFeatureExtractor: extracted %d keywords, %d entities", len(keywords), len(entities))
        return FeatureSet(keywords=keywords, entities=entities, labels={})


# -- 注册到 FeatureExtractorProducer（实现自注册，新增无需改 producer/build_kernel） ------ #



@FeatureExtractorProducer.register("keyword")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    tokenizer = TokenizerProducer.dep(config, default="whitespace")
    return KeywordFeatureExtractor(tokenizer)
