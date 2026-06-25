"""chunker_impl 实现集：工厂 ChunkerProducer + 各实现。

import 各实现模块即触发其 ``@ChunkerProducer.register(...)`` 自注册；本包只对外暴露工厂 ChunkerProducer。
"""

from importlib import import_module

from common.chunker.base import ChunkerProducer

import_module(".fixed_window_chunker", __name__)
import_module(".recursive_chunker", __name__)

__all__ = ["ChunkerProducer"]
