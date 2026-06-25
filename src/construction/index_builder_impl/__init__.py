"""index_builder_impl 实现集：工厂 IndexBuilderProducer + 各实现。

import 各实现模块即触发其 ``@IndexBuilderProducer.register(...)`` 自注册；本包只对外暴露工厂 IndexBuilderProducer。
"""

from importlib import import_module

from construction.index_builder import IndexBuilderProducer

import_module(".fulltext_index_builder", __name__)
import_module(".vector_index_builder", __name__)
import_module(".hybrid_index_builder", __name__)

__all__ = ["IndexBuilderProducer"]
