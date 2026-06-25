"""abstractor_impl 实现集：工厂 AbstractorProducer + 各实现。

import 各实现模块即触发其 ``@AbstractorProducer.register(...)`` 自注册；本包只对外暴露工厂 AbstractorProducer。
"""

from importlib import import_module

from construction.abstractor import AbstractorProducer

import_module(".concat_abstractor", __name__)
import_module(".llm_abstractor", __name__)

__all__ = ["AbstractorProducer"]
