# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""abstractor_impl 实现集：工厂 AbstractorProducer + 各实现。

import 各实现模块即触发其 ``@AbstractorProducer.register(...)`` 自注册；本包只对外暴露工厂 AbstractorProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.construction.abstractor import AbstractorProducer

try:
    import_module(".concat_abstractor", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".llm_abstractor", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["AbstractorProducer"]
