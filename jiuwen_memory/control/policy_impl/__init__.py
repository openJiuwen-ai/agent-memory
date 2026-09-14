# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""policy_impl 实现集：工厂 PolicyProducer + 各实现。

import 各实现模块即触发其 ``@PolicyProducer.register(...)`` 自注册；本包只对外暴露工厂 PolicyProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.control.policy import PolicyProducer

import_optional(".dict_policy_manager", __name__)

__all__ = ["PolicyProducer"]
