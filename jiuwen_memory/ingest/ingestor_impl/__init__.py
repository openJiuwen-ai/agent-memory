# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ingestor_impl 实现集：工厂 IngestorProducer + 各实现。

import 各实现模块即触发其 ``@IngestorProducer.register(...)`` 自注册；本包只对外暴露工厂 IngestorProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.ingest.ingestor import IngestorProducer

try:
    import_module(".simple_ingestor", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["IngestorProducer"]
