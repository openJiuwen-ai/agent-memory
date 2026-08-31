# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""fusion_impl 实现集：工厂 FusionProducer + 各实现。

import 各实现模块即触发其 ``@FusionProducer.register(...)`` 自注册；本包只对外暴露工厂 FusionProducer。
"""

from importlib import import_module

from jiuwen_memory.storage.fusion import FusionProducer

import_module(".in_memory_fusion_store", __name__)
import_module(".milvus_graph_fusion", __name__)

__all__ = ["FusionProducer"]
