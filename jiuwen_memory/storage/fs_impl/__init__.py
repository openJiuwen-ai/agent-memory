# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""fs_impl 实现集：工厂 FsProducer + 各实现。

import 各实现模块即触发其 ``@FsProducer.register(...)`` 自注册；本包只对外暴露工厂 FsProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.storage.fs import FsProducer

import_optional(".in_memory_fs_store", __name__)
import_optional(".local_fs", __name__)

__all__ = ["FsProducer"]
