# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_impl 实现集：工厂 SecurityProducer + 各实现。

import 各实现模块即触发其 ``@SecurityProducer.register(...)`` 自注册；
本包只对外暴露工厂 SecurityProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.common.security.security import SecurityProducer

import_optional(".local_envelope_security_provider", __name__)

__all__ = ["SecurityProducer"]
