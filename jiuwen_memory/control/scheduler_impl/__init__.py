# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""scheduler_impl 实现集：工厂 SchedulerProducer + 各实现。

import 各实现模块即触发其 ``@SchedulerProducer.register(...)`` 自注册；本包只对外暴露工厂
SchedulerProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.control.scheduler import SchedulerProducer

import_optional(".in_process_scheduler", __name__)
import_optional(".async_timer_scheduler", __name__)

__all__ = ["SchedulerProducer"]
