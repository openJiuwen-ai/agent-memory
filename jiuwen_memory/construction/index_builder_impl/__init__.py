# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""index_builder_impl 实现集：工厂 IndexBuilderProducer + 各实现。

import 各实现模块即触发其 ``@IndexBuilderProducer.register(...)`` 自注册；本包只对外暴露工厂 IndexBuilderProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.construction.index_builder import IndexBuilderProducer

import_optional(".forward_index_builder", __name__)
import_optional(".fulltext_index_builder", __name__)
import_optional(".vector_index_builder", __name__)
import_optional(".hybrid_index_builder", __name__)
import_optional(".unified_index_builder", __name__)
import_optional(".entity_index_builder", __name__)

__all__ = ["IndexBuilderProducer"]
