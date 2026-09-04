# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""EntityNormalizer — 归一化实体文本生成稳定 key。

用于去重和精确匹配（hash 精确匹配第一级：normalize+hash）。
归一化规则与原模块一致：strip + lower + 空白折叠。

跨层共享纯函数（storage 写入前归一化 + retrieval 召回前归一化 + construction
编排归一化共用），归口 type_def。
"""

from __future__ import annotations


class EntityNormalizer:
    """归一化实体文本生成稳定 key，用于去重和精确匹配。"""

    @staticmethod
    def normalize(text: str) -> str:
        return " ".join(text.strip().lower().split())
