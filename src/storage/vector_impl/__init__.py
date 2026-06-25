"""vector_impl 实现集：工厂 VectorProducer + 各实现。

import 各实现模块即触发其 ``@VectorProducer.register(...)`` 自注册；本包只对外暴露工厂 VectorProducer。
"""

from importlib import import_module

from storage.vector import VectorProducer

import_module(".in_memory_vector_store", __name__)
import_module(".milvus_vector", __name__)

__all__ = ["VectorProducer"]
