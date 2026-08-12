"""ingestor_impl 实现集：工厂 IngestorProducer + 各实现。

import 各实现模块即触发其 ``@IngestorProducer.register(...)`` 自注册；本包只对外暴露工厂 IngestorProducer。
"""

from importlib import import_module

from jiuwen_memory.ingest.ingestor import IngestorProducer

import_module(".simple_ingestor", __name__)

__all__ = ["IngestorProducer"]
