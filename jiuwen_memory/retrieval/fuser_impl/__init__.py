# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""fuser_impl 实现集：工厂 FuserProducer + 各实现。

import 各实现模块即触发其 ``@FuserProducer.register(...)`` 自注册；本包只对外暴露工厂
FuserProducer。
"""

from importlib import import_module

from jiuwen_memory.retrieval.fuser import FuserProducer

import_module(".rrf_fuser", __name__)
import_module(".weighted_rrf_fuser", __name__)
import_module(".score_max_fuser", __name__)

__all__ = ["FuserProducer"]
