"""markdown_impl 实现集：工厂 MarkdownProducer + 各实现。

import 各实现模块即触发其 ``@MarkdownProducer.register(...)`` 自注册；本包只对外暴露工厂 MarkdownProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.storage.markdown import MarkdownProducer

import_optional(".local_markdown_store", __name__)

__all__ = ["MarkdownProducer"]
