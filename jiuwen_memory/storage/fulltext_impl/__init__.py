"""fulltext_impl 实现集：工厂 FulltextProducer + 各实现。

import 各实现模块即触发其 ``@FulltextProducer.register(...)`` 自注册；本包只对外暴露工厂 FulltextProducer。
"""

from importlib import import_module

from jiuwen_memory.storage.fulltext import FulltextProducer

import_module(".elasticsearch_fulltext", __name__)
import_module(".in_memory_fulltext_store", __name__)

__all__ = ["FulltextProducer"]
