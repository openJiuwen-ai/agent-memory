# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""fuser_impl 实现集：工厂 FuserProducer + 各实现。

import 各实现模块即触发其 ``@FuserProducer.register(...)`` 自注册；本包只对外暴露工厂
FuserProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.retrieval.fuser import FuserProducer

import_optional(".rrf_fuser", __name__)
import_optional(".weighted_rrf_fuser", __name__)
import_optional(".score_max_fuser", __name__)

__all__ = ["FuserProducer"]
