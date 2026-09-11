# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""fulltext_impl 实现集：工厂 FulltextProducer + 各实现。

import 各实现模块即触发其 ``@FulltextProducer.register(...)`` 自注册；本包只对外暴露工厂 FulltextProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.storage.fulltext import FulltextProducer

try:
    import_module(".elasticsearch_fulltext", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".in_memory_fulltext_store", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["FulltextProducer"]
