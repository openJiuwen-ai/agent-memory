"""RawPayload — 信息源产出的原始负载（规约前的形态）。

接入层从各信息源拉到的最原始数据：二进制或其引用 + 模态 + 来源信息。
经 Normalizer 规约出文本投影后转换为 MemoryUnit；落盘（真源与资产）
由构建层负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .memory import Modality
from .scope import Scope


@dataclass
class RawPayload:
    id: str = ""  # 负载 id
    scope: Scope = field(default_factory=Scope)  # 归属 scope
    modality: Modality = Modality.TEXT  # 来源模态
    data: bytes = b""  # 原始字节；与 uri 二选一
    uri: str = ""  # 原始数据的外部引用（文件路径/URL/对象存储 key）
    metadata: dict[str, str] = field(default_factory=dict)  # 来源附加信息
    occurred_at: datetime | None = None  # 发生时间（写入 temporal.t_event）
