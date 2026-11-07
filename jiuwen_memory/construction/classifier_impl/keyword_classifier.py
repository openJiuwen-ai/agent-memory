# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~construction.classifier.Classifier`。

按内容关键词的轻量启发式判定认知角色 tier，并打一个主题标签写入 ``tags``：
- 含偏好/习惯词（喜欢/偏好/习惯…）→ SEMANTIC（知道什么）；
- 含流程词（步骤/流程/先…然后/如何…）→ PROCEDURAL（怎么做）；
- 其余维持 EPISODIC（发生过什么）。
真实实现会用 LLM / FeatureExtractor 做多维分类，这里仅作可复现的占位。

重要度（F10）同款按词判类别（偏好词→preference、规则词→rule、其余 default），按
``importance_category_map`` 查表补写；调用方已显式给值时不覆盖。
"""

from __future__ import annotations

from jiuwen_memory.common.log import get_logger, metadata_for_log, redact_for_log
from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.classifier import Classifier, ClassifierProducer
from jiuwen_memory.construction.common import (
    fill_missing_importance,
    importance_category_map_from_config,
)

logger = get_logger(__name__)

_SEMANTIC = ("喜欢", "偏好", "讨厌", "习惯", "总是", "喜爱")
_PROCEDURAL = ("步骤", "流程", "先", "然后", "如何", "怎么", "方法")
_RULE = ("永远不要", "必须", "禁止", "一律")
_TOPIC = {
    "coffee": ("咖啡", "拿铁", "美式", "燕麦奶"),
    "meeting": ("评审", "会议", "团队"),
    "project": ("项目", "系统", "agent"),
}


class KeywordClassifier(Classifier):
    """关键词启发式分类：设定 tier，并追加一个主题标签。"""

    def __init__(self, importance_category_map: dict[str, str] | None = None) -> None:
        self._importance_category_map = importance_category_map

    def operator_type(self) -> OperatorType:
        return OperatorType.CLASSIFIER

    def health(self) -> None:
        return None

    def classify(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        logger.info("KeywordClassifier: received %d units", len(units))
        for unit in units:
            old_tier = unit.tier.value
            old_tags = list(unit.tags)
            unit.tier = self._tier(unit.content)
            topic = self._topic(unit.content)
            if topic and topic not in unit.tags:
                unit.tags.append(topic)
            fill_missing_importance(
                unit, self._importance_category(unit.content), self._importance_category_map
            )
            logger.info(
                "KeywordClassifier: unit id=%s content=%s → tier=%s→%s, "
                "topic=%s, tags=%s→%s",
                unit.id[:8],
                redact_for_log(unit.content),
                old_tier,
                unit.tier.value,
                metadata_for_log(topic),
                metadata_for_log(old_tags),
                metadata_for_log(unit.tags),
            )
        logger.info("KeywordClassifier: classified %d units", len(units))
        return units

    def _tier(self, content: str) -> MemoryTier:
        if any(w in content for w in _SEMANTIC):
            return MemoryTier.SEMANTIC
        if any(w in content for w in _PROCEDURAL):
            return MemoryTier.PROCEDURAL
        return MemoryTier.EPISODIC

    @staticmethod
    def _importance_category(content: str) -> str:
        if any(w in content for w in _RULE):
            return "rule"
        if any(w in content for w in _SEMANTIC):
            return "preference"
        return "default"

    def _topic(self, content: str) -> str:
        for topic, words in _TOPIC.items():
            if any(w in content for w in words):
                return topic
        return ""


# -- 注册到 ClassifierProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@ClassifierProducer.register("keyword")
def _build(config):
    return KeywordClassifier(
        importance_category_map=importance_category_map_from_config(config),
    )
