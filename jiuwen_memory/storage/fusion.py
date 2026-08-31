# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""FusionStore — 向量·倒排·正排融合存储。

同一 id 的一行 :class:`FusionRecord` 同时承载：

- **向量**（ANN 召回）
- **文本**（倒排/关键词相关性）
- **标量字段**（结构化过滤）
- **原始值**（正排：按 id 点读）

``search`` 在一次调用内完成「向量近邻 AND 标量谓词 (+ 文本相关性混合)」
的融合检索，调用方无需分库召回再做 join；``get`` 即正排读取。``scope`` 为显式
第一入参：写入按 ``scope`` 落库，``search`` / 按 id 的 ``get`` / ``delete`` 物理
约束在该 ``scope`` 内。``id`` 为 scope 内逻辑主键。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope

from .base import BaseStore
from .types import FusionQuery, FusionRecord, ScoredID


class FusionProducer(Factory):
    """FusionStore 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即后端名（如 memory / milvus_graph）。各实现在 ``fusion_impl`` 下以
    ``@FusionProducer.register("<后端>")`` 自注册——注册发生在 import 实现模块时，由
    :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "fusion_store"


class FusionStore(BaseStore):
    @abstractmethod
    def insert(self, scope: Scope, records: list[FusionRecord]) -> None:
        """在 ``scope`` 下新建融合行（向量/文本/标量/正排值一次写入）；id 已存在时报冲突。"""

    @abstractmethod
    def update(self, scope: Scope, records: list[FusionRecord]) -> None:
        """替换已有融合行；id 不存在时报缺失。"""

    @abstractmethod
    def delete(self, scope: Scope, ids: list[str]) -> None:
        """在 ``scope`` 内按 id 删除融合行（幂等）。"""

    @abstractmethod
    def get(self, scope: Scope, ids: list[str]) -> list[FusionRecord]:
        """在 ``scope`` 内正排点查：按 id 读取完整融合行；缺失的 id 从结果中省略。"""

    @abstractmethod
    def search(self, scope: Scope, query: FusionQuery) -> list[ScoredID]:
        """在 ``scope`` 内做融合检索：向量 ANN 受 ``scalar_filters`` 约束，可选与
        文本相关性按 ``query.vector_weight`` 混合，返回 top-k。
        """
