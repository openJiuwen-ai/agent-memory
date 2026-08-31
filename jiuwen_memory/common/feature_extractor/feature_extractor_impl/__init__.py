# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""feature_extractor_impl 实现集：工厂 FeatureExtractorProducer + 各实现。

import 各实现模块即触发其 ``@FeatureExtractorProducer.register(...)`` 自注册；本包只对外暴露工厂 FeatureExtractorProducer。
"""

from importlib import import_module

from jiuwen_memory.common.feature_extractor.base import FeatureExtractorProducer

import_module(".keyword_feature_extractor", __name__)
import_module(".spacy_feature_extractor", __name__)
import_module(".hanlp_feature_extractor", __name__)

__all__ = ["FeatureExtractorProducer"]
