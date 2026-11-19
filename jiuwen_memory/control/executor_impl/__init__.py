# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""executor_impl 实现集：import 触发 BlockingExecutorProducer 自注册。"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.control.executor import BlockingExecutorProducer

import_optional(".thread_pool", __name__)

__all__ = ["BlockingExecutorProducer"]
