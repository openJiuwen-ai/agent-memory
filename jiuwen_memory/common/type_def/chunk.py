# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Chunk — 内容切分后的连续片段，向量化与建索引的基本单元。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    id: str = ""  # chunk id
    unit_id: str = ""  # 所属 MemoryUnit / 源记录 id
    seq: int = 0  # 在父内容中的顺序（0 起）
    text: str = ""  # 片段文本
    start: int = 0  # 在源内容中的起始字符偏移
    end: int = 0  # 在源内容中的结束字符偏移
    token_count: int = 0  # 片段的 token 数
    metadata: dict[str, Any] = field(default_factory=dict)  # 从父内容透传的元数据
