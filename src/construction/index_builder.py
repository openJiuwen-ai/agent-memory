"""IndexBuilder — 多形式索引构建（架构 §6.2）。

在各粒度记忆之上构建/更新索引（文档/关键词/向量/图，按配置启用）。
索引是可配置的检索结构、并非记忆固有结构，全部可从真源重建。
本算子负责构建**逻辑**（调用 Chunker/Tokenizer/Embedder 等共享插件
生成索引投影），持久化由注入的 ``src/storage`` 后端承担——构建与
存储经此解耦。构建各索引记录时把来源 ``MemoryUnit.scope`` 落到记录的
专用 ``scope`` 字段（``VectorRecord``/``Document``/``Node``/``FusionRecord``
等），使检索得以按 scope 原生隔离。
"""

from __future__ import annotations

from abc import abstractmethod

from common.factory.factory import Factory
from common.type_def import MemoryUnit

from .base import ConstructionOperator


class IndexBuilderProducer(Factory):
    """IndexBuilder 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``index_builder_impl`` 下以 ``@IndexBuilderProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "constructor"


class IndexBuilder(ConstructionOperator):
    @abstractmethod
    def build(self, units: list[MemoryUnit]) -> None:
        """为一批记忆单元构建已启用的各形式索引。"""

    @abstractmethod
    def update(self, units: list[MemoryUnit]) -> None:
        """记忆变更后增量更新对应索引条目。"""

    @abstractmethod
    def remove(self, unit_ids: list[str]) -> None:
        """删除一批记忆单元对应的索引条目（幂等）。"""

    @abstractmethod
    def rebuild(self) -> None:
        """从真源全量重建索引（删索引不丢数据的保障）。"""
