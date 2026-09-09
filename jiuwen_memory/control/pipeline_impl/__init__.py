# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""pipeline_impl 实现集：触发 MemoryPipeline 实现自注册。"""

from __future__ import annotations

import logging
from importlib import import_module

try:
    import_module(".metadata_pipeline", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
