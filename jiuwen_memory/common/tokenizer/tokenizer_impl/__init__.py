# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""tokenizer_impl 实现集：工厂 TokenizerProducer + 各实现。

import 各实现模块即触发其 ``@TokenizerProducer.register(...)`` 自注册；本包只对外暴露工厂 TokenizerProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.common.tokenizer.base import TokenizerProducer

try:
    import_module(".whitespace_tokenizer", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".jieba_tokenizer", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["TokenizerProducer"]
