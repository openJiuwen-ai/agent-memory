# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""log_impl 的实现集（注册式工厂 LogProducer + 各实现）。"""

from jiuwen_memory.common._import_support import import_optional

from .log_producer import LogProducer

import_optional(".default_log_setup", __name__)

__all__ = ["LogProducer"]
