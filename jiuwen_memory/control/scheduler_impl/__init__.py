"""scheduler_impl 实现集：工厂 SchedulerProducer + 各实现。

import 各实现模块即触发其 ``@SchedulerProducer.register(...)`` 自注册；本包只对外暴露工厂 SchedulerProducer。
"""

from importlib import import_module

from jiuwen_memory.control.scheduler import SchedulerProducer

import_module(".in_process_scheduler", __name__)
import_module(".async_timer_scheduler", __name__)

__all__ = ["SchedulerProducer"]
