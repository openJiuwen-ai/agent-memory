# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""reranker_impl 实现集：工厂 RerankerProducer + 各实现。

import 各实现模块即触发其 ``@RerankerProducer.register(...)`` 自注册；本包只对外暴露工厂 RerankerProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.common.reranker.base import RerankerProducer

try:
    import_module(".overlap_reranker", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".bge_reranker", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".api_reranker", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["RerankerProducer"]
