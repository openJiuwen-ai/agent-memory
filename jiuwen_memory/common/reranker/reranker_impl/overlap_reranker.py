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
        """初始化 OverlapReranker。

        Args:
            tokenizer: 参数 tokenizer（Tokenizer）。
        """
        self._tokenizer = tokenizer

    def plugin_type(self) -> PluginType:
        """返回当前插件类型。

        Returns:
            返回 PluginType。
        """
        return PluginType.RERANKER

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def rerank(self, query: str, texts: List[str]) -> List[float]:
        """对候选结果重新排序并评分。

        Args:
            query: 参数 query（str）。
            texts: 参数 texts（List[str]）。

        Returns:
            返回 List[float]。
        """
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
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    tokenizer = TokenizerProducer.dep(config, default="whitespace")
    return OverlapReranker(tokenizer)
