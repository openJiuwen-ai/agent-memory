"""recaller_impl 实现集：工厂 RecallerProducer + 各实现。

import 各实现模块即触发其 ``@RecallerProducer.register(...)`` 自注册；本包只对外暴露工厂 RecallerProducer。
"""

from importlib import import_module

from jiuwen_memory.retrieval.recaller import RecallerProducer

import_module(".graph_recaller", __name__)
import_module(".keyword_recaller", __name__)
import_module(".vector_recaller", __name__)

__all__ = ["RecallerProducer"]
