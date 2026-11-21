# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""context_packer_impl 实现集：工厂 ContextPackerProducer + 自适应打包实现。

import 实现模块即触发其 ``@ContextPackerProducer.register(...)`` 自注册。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.retrieval.context_packer import (
    ContextPacker,
    ContextPackerProducer,
    PackStrategy,
)

import_optional(".adaptive_context_packer", __name__)

__all__ = ["ContextPacker", "ContextPackerProducer", "PackStrategy"]
