"""engine_impl 实现集：工厂 EngineProducer + 各实现。

import 各实现模块即触发其 ``@EngineProducer.register(...)`` 自注册；
本包只对外暴露工厂 EngineProducer。
"""

from importlib import import_module

from jiuwen_memory.control.engine import EngineProducer

import_module(".in_memory_engine", __name__)
import_module(".cloud_engine", __name__)

__all__ = ["EngineProducer"]
