# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""log_impl 的实现集（注册式工厂 LogProducer + 各实现）。"""

import logging
from importlib import import_module

from .log_producer import LogProducer

try:
    import_module(".default_log_setup", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["LogProducer"]
