# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""chunker_impl 实现集：工厂 ChunkerProducer + 各实现。

import 各实现模块即触发其 ``@ChunkerProducer.register(...)`` 自注册；本包只对外暴露工厂
ChunkerProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.common.chunker.base import ChunkerProducer

import_optional(".fixed_window_chunker", __name__)
import_optional(".recursive_chunker", __name__)

__all__ = ["ChunkerProducer"]
