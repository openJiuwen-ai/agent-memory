# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""reranker_impl 实现集：工厂 RerankerProducer + 各实现。

import 各实现模块即触发其 ``@RerankerProducer.register(...)`` 自注册；本包只对外暴露工厂
RerankerProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.common.reranker.base import RerankerProducer

import_optional(".overlap_reranker", __name__)
import_optional(".bge_reranker", __name__)
import_optional(".api_reranker", __name__)

__all__ = ["RerankerProducer"]
