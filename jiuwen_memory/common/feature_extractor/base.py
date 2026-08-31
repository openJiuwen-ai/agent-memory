# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""FeatureExtractor — 特征抽取能力：文本 -> 结构化特征。

**共用说明**：产出关键词/命名实体/标签（不含稠密向量，向量由
:class:`~common.embedder.Embedder` 单独产出）。构建层用它富化记忆单元、
为图索引准备实体；检索层用它抽取 query 特征做精确匹配与图召回。
"""

from __future__ import annotations

from abc import abstractmethod

from ..factory.factory import Factory
from ..base import Plugin
from ..type_def import FeatureSet


class FeatureExtractorProducer(Factory):
    """FeatureExtractor 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``feature_extractor_impl`` 下以 ``@FeatureExtractorProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`common.bootstrap.register_plugins` 统一触发。
    """

    TOP_NAME = "feature_extractor"


class FeatureExtractor(Plugin):
    @abstractmethod
    def extract(self, text: str) -> FeatureSet:
        """从 ``text`` 抽取结构化特征（关键词、实体、标签）。"""

    def extract_batch(self, texts: list[str]) -> list[FeatureSet]:
        """批量抽取；默认逐条调用 :meth:`extract`，后端可覆写提速。"""
        return [self.extract(t) for t in texts]
