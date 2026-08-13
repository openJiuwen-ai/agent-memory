"""Ingestor — 接入编排（架构 §10.1 / §14 Write 路径的前半段）。

对一批 :class:`~common.type_def.RawPayload` 执行接入两步：
记录原模态资产引用（assets）→ Normalizer 规约出文本/结构投影
（content），再转换为 :class:`~common.type_def.MemoryUnit` 返回。
**本层不落盘**：返回的记忆单元交给构建层，由构建层负责写入真源并
构建索引。Normalizer 由装配注入。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, RawPayload

from .base import IngestOperator


class IngestorProducer(Factory):
    """Ingestor 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``ingestor_impl`` 下以 ``@IngestorProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`ingest.bootstrap.register_ingestors` 统一触发。
    """

    TOP_NAME = "ingestor"


class Ingestor(IngestOperator):
    @abstractmethod
    def ingest(self, payloads: list[RawPayload]) -> list[MemoryUnit]:
        """接入一批原始负载：记录资产引用 + 规约投影 + 转换为记忆单元（不落盘）。"""
