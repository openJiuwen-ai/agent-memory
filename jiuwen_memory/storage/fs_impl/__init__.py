# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""fs_impl 实现集：工厂 FsProducer + 各实现。

import 各实现模块即触发其 ``@FsProducer.register(...)`` 自注册；本包只对外暴露工厂 FsProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.storage.fs import FsProducer

try:
    import_module(".in_memory_fs_store", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".local_fs", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["FsProducer"]
