"""最小实现：:class:`~construction.classifier.Classifier`。

按内容关键词的轻量启发式判定认知角色 tier，并打一个主题标签写入 ``tags``：
- 含偏好/习惯词（喜欢/偏好/习惯…）→ SEMANTIC（知道什么）；
- 含流程词（步骤/流程/先…然后/如何…）→ PROCEDURAL（怎么做）；
- 其余维持 EPISODIC（发生过什么）。
真实实现会用 LLM / FeatureExtractor 做多维分类，这里仅作可复现的占位。
"""

from __future__ import annotations

from typing import List

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.classifier import Classifier, ClassifierProducer

logger = get_logger(__name__)

_SEMANTIC = ("喜欢", "偏好", "讨厌", "习惯", "总是", "喜爱")
_PROCEDURAL = ("步骤", "流程", "先", "然后", "如何", "怎么", "方法")
_TOPIC = {
    "coffee": ("咖啡", "拿铁", "美式", "燕麦奶"),
    "meeting": ("评审", "会议", "团队"),
    "project": ("项目", "系统", "agent"),
}


class KeywordClassifier(Classifier):
    """关键词启发式分类：设定 tier，并追加一个主题标签。"""

    def operator_type(self) -> OperatorType:
        """返回当前算子类型。

        Returns:
            返回 OperatorType。
        """
        return OperatorType.CLASSIFIER

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def classify(self, units: List[MemoryUnit]) -> List[MemoryUnit]:
        """执行 `classify` 操作。

        Args:
            units: 参数 units（List[MemoryUnit]）。

        Returns:
            返回 List[MemoryUnit]。
        """
        logger.info("KeywordClassifier: received %d units", len(units))
        for unit in units:
            old_tier = unit.tier.value
            old_tags = list(unit.tags)
            unit.tier = self._tier(unit.content)
            topic = self._topic(unit.content)
            if topic and topic not in unit.tags:
                unit.tags.append(topic)
            logger.info("KeywordClassifier: unit id=%s content=%s → tier=%s→%s, topic=%s, tags=%s→%s",
                         unit.id[:8], unit.content[:200], old_tier, unit.tier.value,
                         topic, old_tags, unit.tags)
        logger.info("KeywordClassifier: classified %d units", len(units))
        return units

    def _tier(self, content: str) -> MemoryTier:
        """执行 `tier` 操作。

        Args:
            content: 参数 content（str）。

        Returns:
            返回 MemoryTier。
        """
        if any(w in content for w in _SEMANTIC):
            return MemoryTier.SEMANTIC
        if any(w in content for w in _PROCEDURAL):
            return MemoryTier.PROCEDURAL
        return MemoryTier.EPISODIC

    def _topic(self, content: str) -> str:
        """执行 `topic` 操作。

        Args:
            content: 参数 content（str）。

        Returns:
            返回 str。
        """
        for topic, words in _TOPIC.items():
            if any(w in content for w in words):
                return topic
        return ""


# -- 注册到 ClassifierProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@ClassifierProducer.register("keyword")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return KeywordClassifier()
