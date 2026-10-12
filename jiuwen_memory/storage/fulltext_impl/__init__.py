# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""fulltext_impl 实现集：工厂 FulltextProducer + 各实现。

import 各实现模块即触发其 ``@FulltextProducer.register(...)`` 自注册；本包只对外暴露工厂
FulltextProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.storage.fulltext import FulltextProducer

import_optional(".elasticsearch_fulltext", __name__)
import_optional(".in_memory_fulltext_store", __name__)

__all__ = ["FulltextProducer"]
