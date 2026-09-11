# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""classifier_impl 实现集：工厂 ClassifierProducer + 各实现。

import 各实现模块即触发其 ``@ClassifierProducer.register(...)`` 自注册；本包只对外暴露工厂 ClassifierProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.construction.classifier import ClassifierProducer

try:
    import_module(".keyword_classifier", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".llm_classifier", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["ClassifierProducer"]
