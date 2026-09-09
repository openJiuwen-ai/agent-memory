# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_impl 实现集：工厂 SecurityProducer + 各实现。

import 各实现模块即触发其 ``@SecurityProducer.register(...)`` 自注册；
本包只对外暴露工厂 SecurityProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.common.security.security import SecurityProducer

try:
    import_module(".local_envelope_security_provider", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["SecurityProducer"]
