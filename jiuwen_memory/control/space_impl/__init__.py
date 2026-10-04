# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""space_impl 实现集：import 触发 SpaceProducer 自注册。"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.control.space import SpaceProducer

import_optional(".kv_space_manager", __name__)

__all__ = ["SpaceProducer"]
