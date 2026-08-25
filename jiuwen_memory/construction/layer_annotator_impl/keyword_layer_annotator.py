"""KeywordLayerAnnotator — 规则版分层标注（hot path 可用，不依赖 LLM）。

按规则从 content 提取 L0/L1：
- L1：取 content 前 N 字（默认 200）。
- L0：``tags`` + content 首句；content ≤ 100 字时 ``L0 = content``。

仅对 ``len(content) > layers_threshold`` 的 unit 标注，短 content 留空（由披露端 fallback）。
规则能力有限时允许退化为空，不阻断。
"""

from __future__ import annotations

from typing import List

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.layer_annotator import LayerAnnotator, LayerAnnotatorProducer

logger = get_logger(__name__)

# L1 默认取 content 前 N 字。
_L1_DEFAULT_CHARS = 200
# content ≤ 此长度时 L0 直接等于 content。
_L0_FULL_CONTENT_THRESHOLD = 100


class KeywordLayerAnnotator(LayerAnnotator):
    """规则版分层标注：前 N 字 + 首句，不调 LLM。"""

    def __init__(self, *, layers_threshold: int = 512, l1_chars: int = _L1_DEFAULT_CHARS) -> None:
        """初始化 KeywordLayerAnnotator。

        Args:
            layers_threshold: 参数 layers_threshold（int）。
            l1_chars: 参数 l1_chars（int）。
        """
        super().__init__(layers_threshold=layers_threshold)
        self._l1_chars = l1_chars

    @staticmethod
    def first_sentence(content: str) -> str:
        """取 content 第一句（按句号/换行切分）。

        遍历所有分隔符收集最早出现位置，按最早位置截取——而非按分隔符类型顺序
        在首个被找到的类型处返回（否则中英混合文本如 "Hello. 这是中文。" 会错截到
        第一个中文句号而非更早的英文句号）。
        """
        separators = ("。", "！", "？", ".", "!", "?", "\n")
        earliest = -1
        for sep in separators:
            idx = content.find(sep)
            if idx >= 0 and (earliest == -1 or idx < earliest):
                earliest = idx
        if earliest >= 0:
            return content[: earliest + 1].strip()
        return content[:80].strip()

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def annotate(self, units: List[MemoryUnit]) -> List[MemoryUnit]:
        """执行 `annotate` 操作。

        Args:
            units: 参数 units（List[MemoryUnit]）。

        Returns:
            返回 List[MemoryUnit]。
        """
        logger.info("KeywordLayerAnnotator: received %d units", len(units))
        annotated = 0
        for unit in units:
            if not self._should_annotate(unit):
                continue  # 短 content 留空
            try:
                self._annotate_one(unit)
                annotated += 1
            except Exception as exc:
                logger.warning(
                    "KeywordLayerAnnotator: annotate failed for %s: %s, leave empty",
                    unit.id[:8], exc,
                )
        logger.info(
            "KeywordLayerAnnotator: annotated %d/%d units (threshold=%d)",
            annotated, len(units), self._layers_threshold,
        )
        return units

    def _annotate_one(self, unit: MemoryUnit) -> None:
        """规则提取 L0/L1，写入 unit.layers。"""
        content = unit.content
        # L1：content 前 N 字
        unit.layers.l1 = content[: self._l1_chars]
        # L0：content ≤ 100 字时直接用 content；否则 tags + 首句
        if len(content) <= _L0_FULL_CONTENT_THRESHOLD:
            unit.layers.l0 = content
        else:
            first_sentence = self.first_sentence(content)
            tags_str = " ".join(unit.tags[:3]) if unit.tags else ""
            unit.layers.l0 = f"{tags_str} {first_sentence}".strip()


# -- 注册到 LayerAnnotatorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@LayerAnnotatorProducer.register("keyword")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return KeywordLayerAnnotator(
        layers_threshold=config.get("layer_annotator_threshold", 512),
        l1_chars=config.get("layer_annotator_l1_chars", _L1_DEFAULT_CHARS),
    )
