# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~ingest.ingestor.Ingestor`。

对每条 ``RawPayload``：调用注入的 Normalizer 规约出 content 文本投影，转换为
``MemoryUnit``（分配 id、写双时间）后返回——**不落盘**（落盘归构建层/控制层）。
``assets`` 由本实现复制到当前产出的 Segment；``tags`` 仍由控制层处理。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from jiuwen_memory.common.normalizer import Normalizer, ensure_normalizer_supports
from jiuwen_memory.common.normalizer.base import NormalizerProducer
from jiuwen_memory.common.time_utils import first_absolute_date
from jiuwen_memory.common.type_def import MemoryUnit, RawPayload, Segment, Temporal
from jiuwen_memory.ingest.base import IngestOperatorType
from jiuwen_memory.ingest.hierarchy_hints import extract_hierarchy_hints
from jiuwen_memory.ingest.ingestor import Ingestor, IngestorProducer


def _now() -> datetime:
    return datetime.now(UTC)


class SimpleIngestor(Ingestor):
    """规约 + 转换为记忆单元（不落盘）。"""

    def __init__(self, normalizer: Normalizer) -> None:
        self._normalizer = normalizer

    @staticmethod
    def operator_type() -> IngestOperatorType:
        return IngestOperatorType.INGESTOR

    @staticmethod
    def health() -> None:
        return None

    def ingest(self, payloads: list[RawPayload]) -> list[MemoryUnit]:
        units: list[MemoryUnit] = []
        for payload in payloads:
            ensure_normalizer_supports(self._normalizer, payload.modality)
            now = _now()
            # 叶提示只声明结构身份；在规约前校验，不允许输入声明父子边。
            hierarchy, system_metadata = extract_hierarchy_hints(payload.system_metadata)
            content = self._normalizer.normalize(payload)
            units.append(
                MemoryUnit(
                    id=str(uuid.uuid4()),
                    scope=payload.scope,
                    segments=[
                        Segment(
                            content=content,
                            assets=list(payload.assets),
                            source=payload.modality,
                        )
                    ],
                    source_ref=payload.id,
                    temporal=Temporal(
                        # 默认直存链路从 content 抽取事件时间（首个绝对日期，无则 None）。
                        # 与查询侧共用 common.time_utils 的同一份规则——否则写入侧标注的
                        # 日期与查询侧解析的窗口对不上，时间窗谓词依旧空转（issue #252）。
                        t_event=first_absolute_date(content, now),
                        t_ingest=now,
                        t_valid=now,
                        t_message=payload.occurred_at,
                    ),
                    system_metadata=system_metadata,
                    user_metadata=dict(payload.user_metadata),
                    hierarchy=hierarchy,
                )
            )
        return units


# -- 注册到 IngestorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@IngestorProducer.register("simple")
def _build(config):
    # Normalizer 经 NormalizerProducer 自取（缺省 passthrough），实例由该 Producer 生成/共享。
    return SimpleIngestor(NormalizerProducer.dep(config, default="passthrough"))
