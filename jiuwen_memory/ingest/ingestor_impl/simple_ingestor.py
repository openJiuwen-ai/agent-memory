# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SimpleIngestor：规约 + 转换为记忆单元（不落盘）。

超大文本预切分：超过 ``max_unit_chars`` 的文本按段落边界聚合为多个 MemoryUnit
（system_metadata 记 ``_part``/``_parts``），避免单 unit 巨体量把全文塞进下游
LLM 分类/抽取 prompt（同步写路径分钟级阻塞：add 500KB → 全文单发 LLM 3×300s
超时 ≈900s 无响应）。按段落聚合基本无信息丢失，单段超长时硬切兜底；
``max_unit_chars <= 0`` 关闭切分，保持旧行为。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from jiuwen_memory.common.normalizer import Normalizer, ensure_normalizer_supports
from jiuwen_memory.common.normalizer.base import NormalizerProducer
from jiuwen_memory.common.type_def import MemoryUnit, RawPayload, Segment, Temporal
from jiuwen_memory.ingest.base import IngestOperatorType
from jiuwen_memory.ingest.ingestor import Ingestor, IngestorProducer


def _now() -> datetime:
    return datetime.now(UTC)


class SimpleIngestor(Ingestor):
    """规约 + 转换为记忆单元（不落盘）；超限文本预切分为多个 unit。"""

    def __init__(self, normalizer: Normalizer, max_unit_chars: int = 8000) -> None:
        self._normalizer = normalizer
        self._max_unit_chars = max_unit_chars

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
            content = self._normalizer.normalize(payload)
            pieces = self._split(content)
            total = len(pieces)
            for part, piece in enumerate(pieces, start=1):
                system_metadata = dict(payload.system_metadata)
                if total > 1:
                    system_metadata.setdefault("_part", part)
                    system_metadata.setdefault("_parts", total)
                units.append(
                    MemoryUnit(
                        id=str(uuid.uuid4()),
                        scope=payload.scope,
                        segments=[
                            Segment(
                                content=piece,
                                assets=list(payload.assets),
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
                        system_metadata=system_metadata,
                        user_metadata=dict(payload.user_metadata),
                    )
                )
        return units

    def _split(self, content: str) -> list[str]:
        """按段落边界聚合为 ≤ max_unit_chars 的片段（无信息丢失，可逆拼接）。

        max_unit_chars <= 0 或文本未超限时返回单片段；单段超长时硬切兜底，
        硬切点可能引入一个段落分隔（不影响记忆语义）。
        """
        limit = self._max_unit_chars
        if limit <= 0 or len(content) <= limit:
            return [content]
        pieces: list[str] = []
        buf = ""
        for para in content.split("\n\n"):
            candidate = f"{buf}\n\n{para}" if buf else para
            if len(candidate) <= limit:
                buf = candidate
                continue
            if buf:
                pieces.append(buf)
                buf = ""
            while len(para) > limit:
                pieces.append(para[:limit])
                para = para[limit:]
            buf = para
        if buf:
            pieces.append(buf)
        return pieces


# -- 注册到 IngestorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@IngestorProducer.register("simple")
def _build(config):
    # Normalizer 经 NormalizerProducer 自取（缺省 passthrough）；max_unit_chars
    # 经 globals 下发（与 chunk_size / embedder_max_batch 同通道）。
    return SimpleIngestor(
        NormalizerProducer.dep(config, default="passthrough"),
        max_unit_chars=int(config.get("max_unit_chars", 8000) or 0),
    )
