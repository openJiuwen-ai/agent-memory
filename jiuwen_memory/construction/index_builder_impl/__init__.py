# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""index_builder_impl 实现集：工厂 IndexBuilderProducer + 各实现。

import 各实现模块即触发其 ``@IndexBuilderProducer.register(...)`` 自注册；本包只对外暴露工厂 IndexBuilderProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.construction.index_builder import IndexBuilderProducer

try:
    import_module(".forward_index_builder", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".fulltext_index_builder", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".vector_index_builder", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".hybrid_index_builder", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".unified_index_builder", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".entity_index_builder", __name__)  # entity 子 builder（被 HybridIndexBuilder 组合，不自注册）
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["IndexBuilderProducer"]
