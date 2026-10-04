# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""tokenizer_impl 实现集：工厂 TokenizerProducer + 各实现。

import 各实现模块即触发其 ``@TokenizerProducer.register(...)`` 自注册；本包只对外暴露工厂 TokenizerProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.common.tokenizer.base import TokenizerProducer

import_optional(".whitespace_tokenizer", __name__)
import_optional(".jieba_tokenizer", __name__)

__all__ = ["TokenizerProducer"]
