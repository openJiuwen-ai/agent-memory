# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~construction.associator.Associator`。

按「共享关键词」发现记忆单元间的关联：用 FeatureExtractor 抽各单元关键词，两两比较
重叠达到阈值即产出一条 ``relation="related"`` 的 :class:`~common.type_def.Relation`
（``score`` 取重叠词数，``metadata`` 记下共享词）。真实实现会做实体共指 / 因果 /
引用链，这里用关键词重叠作可复现的占位。FeatureExtractor 与检索侧共用同套特征。
"""

from __future__ import annotations

from typing import List

from jiuwen_memory.common.feature_extractor.base import FeatureExtractor, FeatureExtractorProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit, Relation
from jiuwen_memory.construction.associator import Associator, AssociatorProducer
from jiuwen_memory.construction.base import OperatorType

logger = get_logger(__name__)


class KeywordAssociator(Associator):
    """共享关键词关联：重叠词数 ≥ ``min_overlap`` 即建一条 related 关联。"""

    def __init__(self, feature_extractor: FeatureExtractor, min_overlap: int = 2) -> None:
        self._features = feature_extractor
        self._min_overlap = min_overlap

    def operator_type(self) -> OperatorType:
        return OperatorType.ASSOCIATOR

    def health(self) -> None:
        return None

    def associate(self, units: List[MemoryUnit]) -> List[Relation]:
        logger.info("KeywordAssociator: received %d units", len(units))
        for u in units:
            logger.info(
                "KeywordAssociator: input unit id=%s tier=%s content=%s",
                u.id[:8],
                u.tier.value,
                u.content[:200],
            )
        toks = {u.id: set(self._features.extract(u.content).keywords) for u in units}
        logger.info(
            "KeywordAssociator: keywords extracted — %s",
            {uid[:8]: sorted(kws) for uid, kws in toks.items()},
        )
        relations: List[Relation] = []
        for index, a in enumerate(units):
            remaining_units = units[index + 1:]
            for b in remaining_units:
                shared = toks[a.id] & toks[b.id]
                if len(shared) >= self._min_overlap:
                    relations.append(
                        Relation(
                            source_id=a.id,
                            target_id=b.id,
                            relation="related",
                            score=float(len(shared)),
                            metadata={"shared": ",".join(sorted(shared))},
                        )
                    )
                    logger.info(
                        "KeywordAssociator: found relation %s↔%s shared=%s score=%.1f",
                        a.id[:8],
                        b.id[:8],
                        sorted(shared),
                        float(len(shared)),
                    )
        logger.info("KeywordAssociator: found %d relations total", len(relations))
        return relations


# -- 注册到 AssociatorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@AssociatorProducer.register("keyword")
def _build(config):
    return KeywordAssociator(FeatureExtractorProducer.dep(config, default="keyword"))
