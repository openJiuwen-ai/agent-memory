# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SecondaryScorer — 相关性阈值之后的次级排序算子（retrieval F09 决策 9）。

``PipelineRetriever`` 在 threshold 之后、top_k 截断之前调用本算子：对已通过相关性
门槛的候选做带内重排（如按重要度 / 时效性 tie-break）。本算子与
:class:`~retrieval.fuser.Fuser` 平级但不是 Fuser——Fuser 在 recall 之后、rerank 之前
按 unit_id 融合多路候选；本算子只对单路已排好序的候选做带内重排。

算分算法与业务相关：业务实现本契约并以 ``@SecondaryScorerProducer.register("<名>")``
自注册，在 YAML 把 ``secondary_scorer.default.target`` 指向该名即可替换默认实现。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import ScoredCandidate
from jiuwen_memory.retrieval.base import RetrievalOperator
from jiuwen_memory.retrieval.types import ParsedQuery


class SecondaryScorerProducer(Factory):
    """SecondaryScorer 的注册式工厂（顶层命名空间 ``secondary_scorer``）。"""

    TOP_NAME = "secondary_scorer"


class SecondaryScorer(RetrievalOperator):
    """次级排序契约：只在带内重排，不增删候选、不改变带间相关性顺序。"""

    def explain(self) -> dict[str, str]:
        """生效参数明细，记入 ``secondary_rank`` 轨迹；业务实现可不覆盖，默认为空。"""
        return {}

    @abstractmethod
    def score(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
        *,
        calibrated: bool,
    ) -> list[ScoredCandidate]:
        """对已通过 threshold 的候选做带内重排，返回重排后的候选列表。

        - ``calibrated=False`` 时直接返回 ``candidates`` 不变（未精排路径门控；编排层也会门控）；
        - 候选数 ≤ 1 时直接返回；
        - 入参已按相关性分降序；返回值只在带内调整先后。
        """

    def score_with_detail(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
        *,
        calibrated: bool,
    ) -> tuple[list[ScoredCandidate], dict[str, str]]:
        """``score`` 并附带轨迹明细；业务实现可只实现 :meth:`score`，明细为空。"""
        return self.score(query, candidates, calibrated=calibrated), {}
