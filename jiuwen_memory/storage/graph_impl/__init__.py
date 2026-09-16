# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""graph_impl 实现集：工厂 GraphProducer + 各实现。

import 各实现模块即触发其 ``@GraphProducer.register(...)`` 自注册；本包只对外暴露工厂
GraphProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.storage.graph import GraphProducer

import_optional(".in_memory_graph_store", __name__)
import_optional(".nano_graphrag_graph", __name__)

__all__ = ["GraphProducer"]
