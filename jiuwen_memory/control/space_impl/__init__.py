# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""space_impl 实现集：import 触发 SpaceProducer 自注册。"""

import logging
from importlib import import_module

from jiuwen_memory.control.space import SpaceProducer

try:
    import_module(".kv_space_manager", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["SpaceProducer"]
