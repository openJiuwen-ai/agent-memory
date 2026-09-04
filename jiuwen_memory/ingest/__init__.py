# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""数据接入层（A 层）接口：信息源接入 · 多模态规约 · 转换为记忆单元（不落盘）。"""

from .base import IngestOperator, IngestOperatorType
from .ingestor import Ingestor
from .source import Source

__all__ = [
    "IngestOperator",
    "IngestOperatorType",
    "Source",
    "Ingestor",
]
