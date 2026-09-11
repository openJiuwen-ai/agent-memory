# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""graph_impl 实现集：工厂 GraphProducer + 各实现。

import 各实现模块即触发其 ``@GraphProducer.register(...)`` 自注册；本包只对外暴露工厂 GraphProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.storage.graph import GraphProducer

try:
    import_module(".in_memory_graph_store", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".nano_graphrag_graph", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["GraphProducer"]
