# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""chunker_impl 实现集：工厂 ChunkerProducer + 各实现。

import 各实现模块即触发其 ``@ChunkerProducer.register(...)`` 自注册；本包只对外暴露工厂 ChunkerProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.common.chunker.base import ChunkerProducer

try:
    import_module(".fixed_window_chunker", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".recursive_chunker", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["ChunkerProducer"]
