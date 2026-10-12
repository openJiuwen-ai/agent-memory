# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""governance_impl 实现集：工厂 GovernorProducer + 各实现。

import 各实现模块即触发其 ``@GovernorProducer.register(...)`` 自注册；本包只对外暴露工厂
GovernorProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.control.governance import GovernorProducer

import_optional(".in_memory_governor", __name__)

__all__ = ["GovernorProducer"]
