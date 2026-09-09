# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""extractor_impl 实现集：工厂 ExtractorProducer + 各实现。

import 各实现模块即触发其 ``@ExtractorProducer.register(...)`` 自注册；
本包只对外暴露工厂 ExtractorProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.construction.extractor import ExtractorProducer

try:
    import_module(".keyword_extractor", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".llm_extractor", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".dynamic_llm_extractor", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".video_memory_extractor", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["ExtractorProducer"]
