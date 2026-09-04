# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""retriever_impl 实现集：工厂 RetrieverProducer + 各实现。

import 各实现模块即触发其 ``@RetrieverProducer.register(...)`` 自注册；本包只对外暴露工厂 RetrieverProducer。
"""

from importlib import import_module

from jiuwen_memory.retrieval.retriever import RetrieverProducer

import_module(".pipeline_retriever", __name__)
import_module(".multimodal_retriever", __name__)

__all__ = ["RetrieverProducer"]
