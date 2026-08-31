# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~ingest.ingestor.Ingestor`。

对每条 ``RawPayload``：调用注入的 Normalizer 规约出 content 文本投影，转换为
``MemoryUnit``（分配 id、写双时间）后返回——**不落盘**（落盘归构建层/控制层）。
``assets``/``tags`` 不在此设置（接入层不感知），由上游 write 入参补齐。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List

from jiuwen_memory.common.normalizer import Normalizer
from jiuwen_memory.common.normalizer.base import NormalizerProducer
from jiuwen_memory.common.type_def import MemoryUnit, RawPayload, Segment, Temporal
from jiuwen_memory.ingest.base import IngestOperatorType
from jiuwen_memory.ingest.ingestor import Ingestor, IngestorProducer


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SimpleIngestor(Ingestor):
    """规约 + 转换为记忆单元（不落盘）。"""

    def __init__(self, normalizer: Normalizer) -> None:
        self._normalizer = normalizer

    def operator_type(self) -> IngestOperatorType:
        return IngestOperatorType.INGESTOR

    def health(self) -> None:
        return None

    def ingest(self, payloads: List[RawPayload]) -> List[MemoryUnit]:
        units: List[MemoryUnit] = []
        for payload in payloads:
            now = _now()
            units.append(
                MemoryUnit(
                    id=str(uuid.uuid4()),
                    scope=payload.scope,
                    segments=[
                        Segment(
                            content=self._normalizer.normalize(payload),
                            source=payload.modality,
                        )
                    ],
                    source_ref=payload.id,
                    temporal=Temporal(
                        t_event=None,
                        t_ingest=now,
                        t_valid=now,
                        t_message=payload.occurred_at,
                    ),
                    system_metadata=dict(payload.system_metadata),
                    user_metadata=dict(payload.user_metadata),
                )
            )
        return units


# -- 注册到 IngestorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@IngestorProducer.register("simple")
def _build(config):
    # Normalizer 经 NormalizerProducer 自取（缺省 passthrough），实例由该 Producer 生成/共享。
    return SimpleIngestor(NormalizerProducer.dep(config, default="passthrough"))
