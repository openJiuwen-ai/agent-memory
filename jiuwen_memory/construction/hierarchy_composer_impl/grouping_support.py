# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TIME 高层分组共用的正文选择与严格相邻余弦计算，不接触存储或摘要模型。"""

from __future__ import annotations

import math

from jiuwen_memory.common.embedder import Embedder
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyRole, MemoryUnit


def semantic_text(unit: MemoryUnit) -> str:
    """父节点只取正文，跳过代码生成的时间/数量表头；snapshot 保留完整正文。"""
    if unit.hierarchy.role in (HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.EVENT):
        return "\n".join(segment.content for segment in unit.segments[1:])
    return unit.content


def adjacent_similarities(
    ordered: list[MemoryUnit], threshold: float | None, embedder: Embedder | None, stage: str,
) -> list[float | None]:
    """仅启用阈值时调用显式 Embedder，严格校验后返回相邻余弦。"""
    if threshold is None:
        return [None] * max(0, len(ordered) - 1)
    if embedder is None:
        raise ValidationError(f"{stage} similarity_threshold 要求显式配置 embedder")
    vectors = _normalized_vectors(
        embedder.embed([semantic_text(unit) for unit in ordered]), len(ordered), stage,
    )
    return [sum(left * right for left, right in zip(before, after))
            for before, after in zip(vectors, vectors[1:])]


def _normalized_vectors(
    vectors: list[list[float]], count: int, stage: str,
) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != count:
        raise ValidationError(f"{stage} embedder 必须按输入顺序返回全部向量")
    normalized = []
    dimension = None
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValidationError(f"{stage} embedder 返回空或非法向量")
        if dimension is None:
            dimension = len(vector)
        if len(vector) != dimension or any(
            type(value) not in (int, float) or not math.isfinite(value) for value in vector
        ):
            raise ValidationError(f"{stage} embedder 向量维度或数值非法")
        norm = math.hypot(*vector)
        if norm == 0 or not math.isfinite(norm):
            raise ValidationError(f"{stage} embedder 返回零向量或非法范数")
        normalized.append([coordinate / norm for coordinate in vector])
    return normalized
