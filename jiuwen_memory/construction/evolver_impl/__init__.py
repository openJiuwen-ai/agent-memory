# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""evolver_impl 实现集：工厂 EvolverProducer + 各实现。

import 各实现模块即触发其 ``@EvolverProducer.register(...)`` 自注册；本包只对外暴露工厂 EvolverProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.construction.evolver import EvolverProducer

try:
    import_module(".orchestrating_evolver", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".dynamic_evolver", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["EvolverProducer"]
