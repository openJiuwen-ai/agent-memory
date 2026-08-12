"""space_impl 实现集：import 触发 SpaceProducer 自注册。"""

from importlib import import_module

from jiuwen_memory.control.space import SpaceProducer

import_module(".kv_space_manager", __name__)

__all__ = ["SpaceProducer"]
