"""tokenizer_impl 实现集：工厂 TokenizerProducer + 各实现。

import 各实现模块即触发其 ``@TokenizerProducer.register(...)`` 自注册；本包只对外暴露工厂 TokenizerProducer。
"""

from importlib import import_module

from common.tokenizer.base import TokenizerProducer

import_module(".whitespace_tokenizer", __name__)
import_module(".jieba_tokenizer", __name__)

__all__ = ["TokenizerProducer"]
