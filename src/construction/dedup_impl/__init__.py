"""dedup_impl 实现集：工厂 DedupProducer + 各实现。

import 各实现模块即触发其 ``@DedupProducer.register(...)`` 自注册；本包只对外暴露工厂 DedupProducer。
"""

from importlib import import_module

from construction.dedup import DedupProducer

import_module(".keyword_dedup", __name__)
import_module(".vector_dedup", __name__)

__all__ = ["DedupProducer"]
