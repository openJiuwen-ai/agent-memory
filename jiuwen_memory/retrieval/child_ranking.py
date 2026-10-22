# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""父节点原序不变，所有已准入子节点按同一次问题向量稳定排序。"""

from math import hypot, isfinite

from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.errors import BackendError


def child_cosine_order(question: str, contents: list[str], embedder: Embedder) -> list[int]:
    """保留原问题与完整正文；不用 Parser 改写向量或子继承的父分数。"""
    texts = [question, *contents]
    vectors = embedder.embed(texts)
    if len(vectors) != len(texts):
        raise BackendError("子节点排序 embedding 未按输入顺序返回全部向量")
    dimension = embedder.dimension()
    normalized: list[list[float]] = []
    for vector in vectors:
        if len(vector) != dimension or any(not isfinite(value) for value in vector):
            raise BackendError("子节点排序 embedding 向量维度或数值非法")
        norm = hypot(*vector)
        if not norm or not isfinite(norm):
            raise BackendError("子节点排序 embedding 返回零向量或非法范数")
        normalized.append([value / norm for value in vector])
    question_vector, *children = normalized
    scores = [sum(a * b for a, b in zip(question_vector, child)) for child in children]
    return sorted(range(len(children)), key=lambda index: -scores[index])
