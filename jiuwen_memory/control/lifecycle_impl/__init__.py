# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""lifecycle_impl 实现集：工厂 LifecycleProducer + 各实现。

import 各实现模块即触发其 ``@LifecycleProducer.register(...)`` 自注册；本包只对外暴露工厂 LifecycleProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.control.lifecycle import LifecycleProducer

try:
    import_module(".kv_lifecycle_manager", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["LifecycleProducer"]
