# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""lifecycle_impl 实现集：工厂 LifecycleProducer + 各实现。

import 各实现模块即触发其 ``@LifecycleProducer.register(...)`` 自注册；本包只对外暴露工厂 LifecycleProducer。
"""

from importlib import import_module

from jiuwen_memory.control.lifecycle import LifecycleProducer

import_module(".kv_lifecycle_manager", __name__)

__all__ = ["LifecycleProducer"]
