# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""classifier_impl 实现集：工厂 ClassifierProducer + 各实现。

import 各实现模块即触发其 ``@ClassifierProducer.register(...)`` 自注册；本包只对外暴露工厂 ClassifierProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.construction.classifier import ClassifierProducer

import_optional(".keyword_classifier", __name__)
import_optional(".llm_classifier", __name__)

__all__ = ["ClassifierProducer"]
