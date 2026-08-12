"""governance_impl 实现集：工厂 GovernorProducer + 各实现。

import 各实现模块即触发其 ``@GovernorProducer.register(...)`` 自注册；本包只对外暴露工厂 GovernorProducer。
"""

from importlib import import_module

from jiuwen_memory.control.governance import GovernorProducer

import_module(".in_memory_governor", __name__)

__all__ = ["GovernorProducer"]
