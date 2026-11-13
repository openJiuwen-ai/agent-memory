# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Selector — 选取算子（架构 §7，mem2.0 §2.4）。

相关性过滤（阈值）之后、截断 ``top_k`` 之前的可选阶段：从通过过滤的候选中
决定返回哪几条、按什么顺序。首版实现是经验条目的选取
（:class:`~retrieval.selector_impl.experience_selector.ExperienceSelector`），
做复现要求、排序分、探索名额与按能力分组四件事。

与 :class:`~retrieval.fuser.Fuser` 的分工：Fuser 把多路召回的候选合并成一份
按相关性排好的列表；Selector 在过滤后的候选上做名额分配——一次返回里探索
名额至多一条、每种能力先各出一条。对话记忆检索不配置 Selector，链路行为
不变。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import ScoredMemoryUnit

from .base import RetrievalOperator
from .types import RetrievalQuery


class SelectorProducer(Factory):
    """Selector 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``selector_impl`` 下以
    ``@SelectorProducer.register("<名>")`` 自注册——注册发生在 import 实现模块时，
    由 :func:`retrieval.bootstrap.register_operators` 统一触发。
    """

    TOP_NAME = "selector"


class Selector(RetrievalOperator):
    @abstractmethod
    def select(
        self, survivors: list[ScoredMemoryUnit], query: RetrievalQuery
    ) -> list[ScoredMemoryUnit]:
        """从通过相关性过滤的候选中选出返回的条目并排序。

        返回重排后的候选，调用方（PipelineRetriever）截断前 ``top_k`` 条即最终
        返回的条目；被本算子淘汰的候选不出现。不原地修改入参：需要改写条目
        （如经验条目的引用值）时在候选上换条目副本，因为存储后端返回的可能是
        缓存中的同一对象。
        """
