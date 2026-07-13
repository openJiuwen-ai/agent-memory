"""LayerAnnotator — 分层披露标注（架构 §9.1）。

给已有 ``MemoryUnit`` 标注 L0/L1 内容层（``unit.layers``），供检索披露端渐进消费。
L0=50-100 字概要、L1=200-500 字要点 overview。L2 不存（``unit.content`` 即 L2）。

与 Extractor/Abstractor 的边界：本算子不产出新记忆，只给已有 unit 写 ``layers``。
生成时机由 Evolver 编排——EXTRACT/CONSOLIDATE 抽取（或升华）产出派生候选后、
去重落盘前调用本算子，保证落盘的 unit 已带 layers（见 F01-memory-layer）。

实现按 ``layers_threshold`` 筛选：仅对 ``len(content) > threshold`` 的 unit 标注
（短 content 不调 LLM、不硬凑摘要，留空由披露端 fallback）。两实现：
- :class:`~construction.layer_annotator_impl.keyword_layer_annotator.KeywordLayerAnnotator`
  规则版（前 N 字 + 首句），hot path 可用。
- :class:`~construction.layer_annotator_impl.llm_layer_annotator.LLMLayerAnnotator`
  LLM 版，background / infer=true 路径用。

best effort：单条/单批标注失败不阻断 write/update/evolve，失败时 layers 留空。
"""

from __future__ import annotations

from abc import abstractmethod

from common.factory.factory import Factory
from common.type_def import MemoryUnit

from .base import ConstructionOperator, OperatorType


class LayerAnnotatorProducer(Factory):
    """LayerAnnotator 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名（如 keyword / llm）。各实现在 ``layer_annotator_impl`` 下以
    ``@LayerAnnotatorProducer.register("<名>")`` 自注册——由
    :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "layer_annotator"


class LayerAnnotator(ConstructionOperator):
    """分层披露标注算子：给 unit 写 ``layers.l0``/``layers.l1``。

    实现负责：按 ``layers_threshold`` 筛选超阈 content 的 unit → 生成 L0/L1 →
    原地写 ``unit.layers``。短 content 不标注（留空）。失败降级为空 layers，
    不阻断演进。
    """

    def __init__(self, *, layers_threshold: int = 512) -> None:
        # content 长度超此阈值的 unit 才标注 L0/L1；短 content 留空。
        self._layers_threshold = layers_threshold

    @abstractmethod
    def annotate(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """为一批 unit 生成 L0/L1 标注，原地写 ``unit.layers``，返回原列表。

        仅对 ``len(content) > layers_threshold`` 的 unit 标注，其余留空。
        实现内部任何异常都应吞掉并保留空 layers——标注是尽力而为，不可阻断演进。
        """

    def operator_type(self) -> OperatorType:
        return OperatorType.LAYER_ANNOTATOR

    def _should_annotate(self, unit: MemoryUnit) -> bool:
        """该 unit 是否需要标注（content 长度超阈值）。"""
        return len(unit.content) > self._layers_threshold
