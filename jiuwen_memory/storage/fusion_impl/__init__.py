# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""fusion_impl 实现集：工厂 FusionProducer + 各实现。

import 各实现模块即触发其 ``@FusionProducer.register(...)`` 自注册；本包只对外暴露工厂 FusionProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.storage.fusion import FusionProducer

try:
    import_module(".in_memory_fusion_store", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".milvus_graph_fusion", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["FusionProducer"]
