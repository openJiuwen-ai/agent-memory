# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""appraiser_impl 实现集：工厂 AppraiserProducer + 各实现。

import 各实现模块即触发其 ``@AppraiserProducer.register(...)`` 自注册；
本包只对外暴露工厂 AppraiserProducer。模块列表：

- ``experience_appraiser``：经验记忆的评价算子（轨迹沉淀八步的 1-5 与 8）
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.construction.appraiser import AppraiserProducer

import_optional(".experience_appraiser", __name__)

__all__ = ["AppraiserProducer"]
