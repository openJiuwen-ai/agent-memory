# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""associator_impl 实现集：工厂 AssociatorProducer + 各实现。

import 各实现模块即触发其 ``@AssociatorProducer.register(...)`` 自注册；本包只对外暴露工厂 AssociatorProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.construction.associator import AssociatorProducer

try:
    import_module(".keyword_associator", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".llm_associator", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["AssociatorProducer"]
