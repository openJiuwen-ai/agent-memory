# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""associator_impl 实现集：工厂 AssociatorProducer + 各实现。

import 各实现模块即触发其 ``@AssociatorProducer.register(...)`` 自注册；本包只对外暴露工厂
AssociatorProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.construction.associator import AssociatorProducer

import_optional(".keyword_associator", __name__)
import_optional(".llm_associator", __name__)

__all__ = ["AssociatorProducer"]
