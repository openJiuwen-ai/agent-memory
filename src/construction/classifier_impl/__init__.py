"""classifier_impl 实现集：工厂 ClassifierProducer + 各实现。

import 各实现模块即触发其 ``@ClassifierProducer.register(...)`` 自注册；本包只对外暴露工厂 ClassifierProducer。
"""

from importlib import import_module

from construction.classifier import ClassifierProducer

import_module(".keyword_classifier", __name__)
import_module(".llm_classifier", __name__)

__all__ = ["ClassifierProducer"]
