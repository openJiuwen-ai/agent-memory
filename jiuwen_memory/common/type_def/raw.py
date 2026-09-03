# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RawPayload — 信息源产出的原始负载（规约前的形态）。

接入层从各信息源拉到的最原始数据：二进制或其引用 + 模态 + 来源信息。
经 Normalizer 规约出文本投影后转换为 MemoryUnit；落盘（真源与资产）
由构建层负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .memory import MetadataValueType, Modality
from .scope import Scope


@dataclass
class RawPayload:
    id: str = ""  # 负载 id
    scope: Scope = field(default_factory=Scope)  # 归属 scope
    modality: Modality = Modality.TEXT  # 来源模态
    data: bytes = b""  # 原始字节；与 uri 二选一
    uri: str = ""  # 原始数据的外部引用（文件路径/URL/对象存储 key）
    system_metadata: dict[str, MetadataValueType] = field(default_factory=dict)
    user_metadata: dict[str, MetadataValueType] = field(default_factory=dict)
    occurred_at: datetime | None = None  # 发生时间（写入 temporal.t_event）
    assets: list[str] = field(default_factory=list)  # 待 Ingestor 映射的资产引用


# -- KV key 前缀（未建索引的 infer 原文） ------------------------------------- #
# 真源 KV key 按「是否建索引」带前缀（见 F02 决策6）：未建索引的 infer=true 原文
# 落 /messages/{id}（建索引记忆落 /memory/{id}，见 memory.py）。所有落盘/回查
# infer 原文的点用 messages_key；拉取最近 N 条做指代消解/语境时用 list(prefix=...)。

MESSAGES_KEY_PREFIX = "/messages/"


def messages_key(unit_id: str) -> str:
    """未建索引 infer 原文的 KV key。"""
    return f"{MESSAGES_KEY_PREFIX}{unit_id}"
