"""FeatureExtractor — 特征抽取能力：文本 -> 结构化特征。

**共用说明**：产出关键词/命名实体/标签（不含稠密向量，向量由
:class:`~common.embedder.Embedder` 单独产出）。构建层用它富化记忆单元、
为图索引准备实体；检索层用它抽取 query 特征做精确匹配与图召回。
"""

from __future__ import annotations

from abc import abstractmethod

from ..base import Plugin
from ..type_def import FeatureSet


class FeatureExtractor(Plugin):
    @abstractmethod
    def extract(self, text: str) -> FeatureSet:
        """从 ``text`` 抽取结构化特征（关键词、实体、标签）。"""

    def extract_batch(self, texts: list[str]) -> list[FeatureSet]:
        """批量抽取；默认逐条调用 :meth:`extract`，后端可覆写提速。"""
        return [self.extract(t) for t in texts]
