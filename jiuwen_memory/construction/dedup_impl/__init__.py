# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""dedup_impl 实现集：工厂 DedupProducer + 各实现。

import 各实现模块即触发其 ``@DedupProducer.register(...)`` 自注册；本包只对外暴露工厂 DedupProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.construction.dedup import DedupProducer

try:
    import_module(".keyword_dedup", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".vector_dedup", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["DedupProducer"]
