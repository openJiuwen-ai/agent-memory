"""graph_impl 实现集：工厂 GraphProducer + 各实现。

import 各实现模块即触发其 ``@GraphProducer.register(...)`` 自注册；本包只对外暴露工厂 GraphProducer。
"""

from importlib import import_module

from jiuwen_memory.storage.graph import GraphProducer

import_module(".in_memory_graph_store", __name__)
import_module(".nano_graphrag_graph", __name__)

__all__ = ["GraphProducer"]
