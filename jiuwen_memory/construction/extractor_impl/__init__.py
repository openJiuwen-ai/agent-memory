# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""extractor_impl 实现集：工厂 ExtractorProducer + 各实现。

import 各实现模块即触发其 ``@ExtractorProducer.register(...)`` 自注册；
本包只对外暴露工厂 ExtractorProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.construction.extractor import ExtractorProducer

import_optional(".keyword_extractor", __name__)
import_optional(".llm_extractor", __name__)
import_optional(".dynamic_llm_extractor", __name__)
import_optional(".video_memory_extractor", __name__)

__all__ = ["ExtractorProducer"]
