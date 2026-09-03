# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""protection_impl 实现集：三个 Protection 工厂 + 各实现。

import 各实现模块即触发其 ``@<X>Producer.register(...)`` 自注册；
本包只对外暴露三个工厂。
"""

from importlib import import_module

from jiuwen_memory.common.security.protection.binding_policy import BindingPolicyProducer
from jiuwen_memory.common.security.protection.rate_limit import RateLimitProducer
from jiuwen_memory.common.security.protection.workload_guard import WorkloadGuardProducer

import_module(".token_bucket_limiter", __name__)
import_module(".unlimited_limiter", __name__)
import_module(".semaphore_guard", __name__)
import_module(".loopback_binding", __name__)

__all__ = ["BindingPolicyProducer", "RateLimitProducer", "WorkloadGuardProducer"]
