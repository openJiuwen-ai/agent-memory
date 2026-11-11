# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""secondary_scorer 实现集：工厂 SecondaryScorerProducer + 契约 + 默认实现。

import 默认实现模块即触发其 ``@SecondaryScorerProducer.register("default")`` 自注册。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.retrieval.fuser_impl.secondary_scorer.secondary_scorer import (
    SecondaryScorer,
    SecondaryScorerProducer,
)

import_optional(".default_secondary_scorer", __name__)

__all__ = ["SecondaryScorer", "SecondaryScorerProducer"]
