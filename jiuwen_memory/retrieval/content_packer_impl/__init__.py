# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""content_packer_impl 实现集：工厂 ContentPackerProducer + 自适应打包实现。

import 实现模块即触发其 ``@ContentPackerProducer.register(...)`` 自注册。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.retrieval.content_packer import (
    ContentPacker,
    ContentPackerProducer,
    PackStrategy,
)

import_optional(".adaptive_content_packer", __name__)

__all__ = ["ContentPacker", "ContentPackerProducer", "PackStrategy"]
