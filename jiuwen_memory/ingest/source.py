# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Source — 多模态信息源连接器（架构 §10）。

对接一类信息源（对话流/文档/代码库/工具调用轨迹/图像/音视频/外部导入），
把源数据拉取为统一的 :class:`~common.type_def.RawPayload`。连接器只负责
「取到原始数据」，不做规约——规约归 Normalizer，编排归 Ingestor。
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime

from jiuwen_memory.common.type_def import Modality, RawPayload

from .base import IngestOperator


class Source(IngestOperator):
    @abstractmethod
    def modalities(self) -> list[Modality]:
        """返回本信息源会产出的模态。"""

    @abstractmethod
    def fetch(self, since: datetime | None = None) -> list[RawPayload]:
        """拉取原始数据；``since`` 非空时增量拉取该时间点之后的新数据。"""
