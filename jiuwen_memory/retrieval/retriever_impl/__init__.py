# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""retriever_impl 实现集：工厂 RetrieverProducer + 各实现。

import 各实现模块即触发其 ``@RetrieverProducer.register(...)`` 自注册；本包只对外暴露工厂 RetrieverProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.retrieval.retriever import RetrieverProducer

try:
    import_module(".pipeline_retriever", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".multimodal_retriever", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["RetrieverProducer"]
