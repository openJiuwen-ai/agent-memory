"""Extractor — 信息提取（架构 §6.1）。

从原始记忆单元（真源中贴近原始的数据）中抽取事实/事件/偏好，产出
**低抽象粒度**的派生记忆单元，``provenance`` 回指来源。真实实现依赖
``common`` 的 LLM / FeatureExtractor / Tokenizer 插件做意图识别、
摘要与特征富化。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit

from .base import ConstructionOperator, ExtractContext


class ExtractorProducer(Factory):
    """Extractor 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``extractor_impl`` 下以 ``@ExtractorProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "extractor"


class Extractor(ConstructionOperator):
    @abstractmethod
    def extract(
        self,
        units: list[MemoryUnit],
        *,
        context: ExtractContext | None = None,
    ) -> list[MemoryUnit]:
        """从一批原始记忆单元中提取零或多条低抽象粒度的派生单元。

        ``context``（infer=true 同步抽取时由 Evolver 透传）只作 prompt 参考：
        ``recent_originals`` 做指代/代词消解与语境，``related_memories`` 提示已有
        事实以规避重复。context 不进提取来源列表——本轮 ``units`` 是唯一提取来源。
        默认 None → 行为与未扩展前一致（向后兼容）。
        """
