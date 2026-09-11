# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""scheduler_impl 实现集：工厂 SchedulerProducer + 各实现。

import 各实现模块即触发其 ``@SchedulerProducer.register(...)`` 自注册；本包只对外暴露工厂 SchedulerProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.control.scheduler import SchedulerProducer

try:
    import_module(".in_process_scheduler", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".async_timer_scheduler", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["SchedulerProducer"]
