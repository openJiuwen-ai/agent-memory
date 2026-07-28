"""extractor_impl 实现集：工厂 ExtractorProducer + 各实现。

import 各实现模块即触发其 ``@ExtractorProducer.register(...)`` 自注册；
本包只对外暴露工厂 ExtractorProducer。
"""

from importlib import import_module

from construction.extractor import ExtractorProducer

import_module(".keyword_extractor", __name__)
import_module(".llm_extractor", __name__)
import_module(".dynamic_llm_extractor", __name__)

__all__ = ["ExtractorProducer"]
