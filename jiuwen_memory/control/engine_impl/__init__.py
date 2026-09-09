# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""engine_impl 实现集：工厂 EngineProducer + 各实现。

import 各实现模块即触发其 ``@EngineProducer.register(...)`` 自注册；
本包只对外暴露工厂 EngineProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.control.engine import EngineProducer

try:
    import_module(".in_memory_engine", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".cloud_engine", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["EngineProducer"]
