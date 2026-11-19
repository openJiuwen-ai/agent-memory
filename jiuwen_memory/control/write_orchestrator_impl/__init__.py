# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""write_orchestrator_impl 实现集：import 触发 WriteOrchestratorProducer 自注册。"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.control.write_orchestrator import WriteOrchestratorProducer

import_optional(".grouped", __name__)
import_optional(".sequential", __name__)

__all__ = ["WriteOrchestratorProducer"]
