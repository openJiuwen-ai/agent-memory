# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""fuser_impl 实现集：工厂 FuserProducer + 各实现。

import 各实现模块即触发其 ``@FuserProducer.register(...)`` 自注册；本包只对外暴露工厂
FuserProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.retrieval.fuser import FuserProducer

try:
    import_module(".rrf_fuser", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".weighted_rrf_fuser", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".score_max_fuser", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["FuserProducer"]
