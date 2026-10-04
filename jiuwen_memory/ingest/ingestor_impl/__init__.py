# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ingestor_impl 实现集：工厂 IngestorProducer + 各实现。

import 各实现模块即触发其 ``@IngestorProducer.register(...)`` 自注册；本包只对外暴露工厂 IngestorProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.ingest.ingestor import IngestorProducer

import_optional(".simple_ingestor", __name__)

__all__ = ["IngestorProducer"]
