"""lock_impl 实现集：工厂 LockProducer + 各实现。

import 各实现模块即触发其 ``@LockProducer.register(...)`` 自注册；本包只对外暴露工厂
LockProducer。redis 实现把客户端 import 推迟到首次建连，故模块导入本身不依赖 redis 包，
无需 try/except 包裹。
"""

from importlib import import_module

from jiuwen_memory.common.lock.lock import LockProducer

import_module(".in_memory_lock", __name__)
import_module(".redis_lock", __name__)

__all__ = ["LockProducer"]
