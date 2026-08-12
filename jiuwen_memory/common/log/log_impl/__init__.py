"""log_impl 的实现集（注册式工厂 LogProducer + 各实现）。"""

from importlib import import_module

from .log_producer import LogProducer

import_module(".default_log_setup", __name__)

__all__ = ["LogProducer"]
