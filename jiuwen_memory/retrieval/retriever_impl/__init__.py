# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""retriever_impl 实现集：工厂 RetrieverProducer + 各实现。

import 各实现模块即触发其 ``@RetrieverProducer.register(...)`` 自注册；本包只对外暴露工厂 RetrieverProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.retrieval.retriever import RetrieverProducer

import_optional(".pipeline_retriever", __name__)
import_optional(".multimodal_retriever", __name__)

__all__ = ["RetrieverProducer"]
