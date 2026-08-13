"""构建层（E 层）算子基类。

构建层的输入是接入层规约/转换产生的 :class:`~common.type_def.MemoryUnit`。
**落盘由本层负责**：构建层调用 ``src/storage`` 各 Store 把记忆单元写入
真源，并在其上挖掘分层记忆、构建多形式索引（架构 §4/§6），由自演进
闭环持续维护（§8）。各环节拆成可插拔算子：

- :class:`~construction.extractor.Extractor` 信息提取（低抽象粒度）
- :class:`~construction.abstractor.Abstractor` 抽象与精炼/升华（高抽象粒度）
- :class:`~construction.associator.Associator` 关联分析（实体/因果/引用）
- :class:`~construction.classifier.Classifier` 多维分类（认知角色/主题/重要度）
- :class:`~construction.index_builder.IndexBuilder` 多形式索引构建
- :class:`~construction.evolver.Evolver` 自演进闭环

算子内部调用 ``src/common`` 共享插件（分词/切分/向量化/特征抽取/LLM），
经 ``src/storage`` 接口落库；二者均由装配注入，算子不依赖具体后端。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from jiuwen_memory.common.type_def.memory import MemoryUnit


class OperatorType(str, Enum):
    EXTRACTOR = "extractor"
    ABSTRACTOR = "abstractor"
    ASSOCIATOR = "associator"
    CLASSIFIER = "classifier"
    INDEX_BUILDER = "index_builder"
    EVOLVER = "evolver"
    LAYER_ANNOTATOR = "layer_annotator"


class ConstructionOperator(ABC):
    """所有构建层算子的自描述契约。"""

    @abstractmethod
    def operator_type(self) -> OperatorType:
        """返回本算子的类型。"""

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。"""


@dataclass
class ExtractContext:
    """infer=true 同步抽取时的上下文参考项（只读，不作为提取来源）。

    见 docs/features/api/F02-write-infer-extract.md「上下文增强抽取」。两类参考项
    职责不同：

    - ``recent_originals``：最近 N 条 infer=true 原始消息（规约后的 MemoryUnit，落
      ``/messages/``，未建索引）。**用于指代/代词消解与语境丰富**——让 extractor
      理解"它/他/那个"指什么、对话背景。只拼进 extractor prompt，**不参与去重**。
    - ``related_memories``：用本轮信息经 dedup.recall 召回的相关记忆（SEMANTIC 派生
      MemoryUnit，落 ``/memory/``）。**用于去重**——既进 prompt 提示已有事实，又经
      产出后相似度过滤丢弃重复候选。

    二者都不进 ``extract()`` 的提取来源列表：本轮 units 才是唯一提取来源。
    放 construction.base（与算子契约同处），extractor/evolver 直接引用。
    """

    recent_originals: list[MemoryUnit] = field(default_factory=list)
    related_memories: list[MemoryUnit] = field(default_factory=list)
