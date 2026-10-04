# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""engine_impl 实现集：工厂 EngineProducer + 各实现。

import 各实现模块即触发其 ``@EngineProducer.register(...)`` 自注册；
本包只对外暴露工厂 EngineProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.control.engine import EngineProducer

import_optional(".in_memory_engine", __name__)
import_optional(".cloud_engine", __name__)

__all__ = ["EngineProducer"]
