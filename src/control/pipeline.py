"""MemoryPipeline — 控制层的跨构建/查询 pipeline 编排抽象。

Pipeline 不实现抽取、索引或检索算法；它只根据写入单元或查询选项选择
一组已装配好的跨层算子绑定。这样 route 发生在 control 编排层，
construction / retrieval 仍保持不知道 control 的单向依赖边界。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from common.factory.factory import Factory
from common.type_def import MemoryUnit
from retrieval.types import RetrievalQuery

from .base import ControlOperator

if TYPE_CHECKING:
    from construction.classifier import Classifier
    from construction.evolver import Evolver
    from construction.index_builder import IndexBuilder
    from retrieval.retriever import Retriever


class PipelineProducer(Factory):
    """MemoryPipeline 的注册式工厂。

    配置顶层名为 ``pipeline``；具体实现通过 ``@PipelineProducer.register("<name>")``
    自注册，并在 control bootstrap 中统一 import。
    """

    TOP_NAME = "pipeline"


@dataclass(frozen=True)
class PipelineBinding:
    """一个 pipeline profile 绑定的一组跨层算子。"""

    name: str
    index_builder: "IndexBuilder"
    retriever: "Retriever"
    evolver: "Evolver"
    classifier: "Classifier | None" = None


class MemoryPipeline(ControlOperator):
    """按写入/查询上下文选择 pipeline profile。"""

    @abstractmethod
    def select_for_write(self, units: list[MemoryUnit]) -> PipelineBinding:
        """根据本次写入产生的 MemoryUnit 选择构建侧 profile。"""

    @abstractmethod
    def select_for_recall(self, query: RetrievalQuery) -> PipelineBinding:
        """根据 RetrievalQuery 选择查询侧 profile。"""
