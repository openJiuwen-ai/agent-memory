# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""vector_impl 实现集：工厂 VectorProducer + 各实现。

import 各实现模块即触发其 ``@VectorProducer.register(...)`` 自注册；本包只对外暴露工厂 VectorProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.storage.vector import VectorProducer

try:
    import_module(".in_memory_vector_store", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".milvus_vector", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".pgvector_vector", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["VectorProducer"]
