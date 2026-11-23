# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""selector_impl 实现集：工厂 SelectorProducer + 各实现。

import 各实现模块即触发其 ``@SelectorProducer.register(...)`` 自注册；本包只对外
暴露工厂 SelectorProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.retrieval.selector import SelectorProducer

import_optional(".experience_selector", __name__)

__all__ = ["SelectorProducer"]
