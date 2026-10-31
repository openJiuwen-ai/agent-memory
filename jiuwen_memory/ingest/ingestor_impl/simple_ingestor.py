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

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.normalizer import Normalizer, ensure_normalizer_supports
from jiuwen_memory.common.normalizer.base import NormalizerProducer
from jiuwen_memory.common.security.privacy.base import split_protected_text
from jiuwen_memory.common.text_split import split_within_limit
from jiuwen_memory.common.time_utils import first_absolute_date
from jiuwen_memory.common.type_def import MemoryUnit, RawPayload, Segment, Temporal
from jiuwen_memory.ingest.base import IngestOperatorType
from jiuwen_memory.ingest.hierarchy_hints import extract_hierarchy_hints
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
            # 叶提示只声明结构身份；在规约前校验，不允许输入声明父子边。
            hierarchy, base_system_metadata = extract_hierarchy_hints(payload.system_metadata)
            content = self._normalizer.normalize(payload)
            pieces = (
                split_protected_text(content, self._max_unit_chars)
                if "privacy_level" in payload.system_metadata
                else self._split(content)
            )
            total = len(pieces)
            # t_event 从完整 content 抽取（首个绝对日期，无则 None），所有分片继承同一份：
            # 切分不改变事件时间，且与查询侧共用 common.time_utils 的同一份规则——否则
            # 写入侧标注的日期与查询侧解析的窗口对不上，时间窗谓词依旧空转（issue #252）。
            t_event = first_absolute_date(content, now)
            # 每个分片继承同一份（消毒后的）结构提示与元数据；多片时记 _part/_parts。
            # 这两个键是 simple 实现的内部序号约定，不对输入开放：用户注入的同名键
            # 一律剔除——多片时强制覆盖为真实序号（setdefault 会让注入值污染全部
            # 分片），单片时不残留。
            for part, piece in enumerate(pieces, start=1):
                system_metadata = dict(base_system_metadata)
                system_metadata.pop("_part", None)
                system_metadata.pop("_parts", None)
                if total > 1:
                    system_metadata["_part"] = part
                    system_metadata["_parts"] = total
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
                            t_event=t_event,
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

    def _split(self, content: str) -> list[str]:
        """按段落边界聚合为 ≤ max_unit_chars 的片段。

        段落聚合路径无损可逆：``"\n\n".join(pieces)`` 严格还原原文（含首部
        空段落与冲刷边界上的空段落分隔符）；单段超长时硬切兜底，硬切点拼接会
        引入一个段落分隔（不影响记忆语义）。max_unit_chars <= 0 或文本未超限
        时返回单片段。算法与 construction 内容分批共用（common.text_split）。
        """
        return split_within_limit(content, self._max_unit_chars)


# -- 注册到 IngestorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


def _parse_max_unit_chars(raw: object) -> int:
    """校验 max_unit_chars 配置：缺省（None）取出厂默认 8000。

    显式 null/空串/非整数值直接拒绝——``or 0`` 式兜底会把它们静默归零、反向
    关闭预切分，与加固意图相反。0 或负数是文档化的关闭开关，不属于非法值。
    """
    if raw is None:
        return 8000
    if isinstance(raw, bool):
        raise ValidationError(f"max_unit_chars 需为整数，得到布尔值 {raw!r}")
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise ValidationError(f"max_unit_chars 需为整数，得到 {raw!r}") from exc


@IngestorProducer.register("simple")
def _build(config):
    # Normalizer 经 NormalizerProducer 自取（缺省 passthrough）；max_unit_chars
    # 经 globals 下发（与 chunk_size / embedder_max_batch 同通道），非法值显式拒绝。
    return SimpleIngestor(
        NormalizerProducer.dep(config, default="passthrough"),
        max_unit_chars=_parse_max_unit_chars(config.get("max_unit_chars")),
    )
