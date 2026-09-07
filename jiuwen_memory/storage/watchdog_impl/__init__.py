"""watchdog_impl 实现集：工厂 WatchdogProducer + 各实现。

import 各实现模块即触发其 ``@WatchdogProducer.register(...)`` 自注册；
本包只对外暴露工厂 WatchdogProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.storage.watchdog import WatchdogProducer

import_optional(".local_watchdog", __name__)

__all__ = ["WatchdogProducer"]
