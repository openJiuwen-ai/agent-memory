# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""vector_impl 实现集：工厂 VectorProducer + 各实现。

import 各实现模块即触发其 ``@VectorProducer.register(...)`` 自注册；本包只对外暴露工厂 VectorProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.storage.vector import VectorProducer

import_optional(".in_memory_vector_store", __name__)
import_optional(".milvus_vector", __name__)
import_optional(".pgvector_vector", __name__)

__all__ = ["VectorProducer"]
