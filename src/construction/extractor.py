"""Extractor — 信息提取（架构 §6.1）。

从原始记忆单元（真源中贴近原始的数据）中抽取事实/事件/偏好，产出
**低抽象粒度**的派生记忆单元，``provenance`` 回指来源。真实实现依赖
``common`` 的 LLM / FeatureExtractor / Tokenizer 插件做意图识别、
摘要与特征富化。
"""

from __future__ import annotations

from abc import abstractmethod

from common.type_def import MemoryUnit

from .base import ConstructionOperator


class Extractor(ConstructionOperator):
    @abstractmethod
    def extract(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """从一批原始记忆单元中提取零或多条低抽象粒度的派生单元。"""
