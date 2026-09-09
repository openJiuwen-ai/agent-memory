# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""kv_impl 实现集：工厂 KvProducer + 各实现。

import 各实现模块即触发其 ``@KvProducer.register(...)`` 自注册；
本包只对外暴露工厂 KvProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.storage.kv import KvProducer

try:
    import_module(".in_memory_kv_store", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".sqlite_kv_store", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".redis_kv", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".encrypted_kv_store", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".postgres_kv", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["KvProducer"]
