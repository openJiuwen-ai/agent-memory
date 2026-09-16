# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""dedup_impl 实现集：工厂 DedupProducer + 各实现。

import 各实现模块即触发其 ``@DedupProducer.register(...)`` 自注册；本包只对外暴露工厂
DedupProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.construction.dedup import DedupProducer

import_optional(".keyword_dedup", __name__)
import_optional(".vector_dedup", __name__)

__all__ = ["DedupProducer"]
