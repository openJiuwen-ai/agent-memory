"""Evolver — 记忆自演进（架构 §8）。

持续驱动「抽取 → 关联 → 冲突消解 → 升华 → 遗忘/降权」闭环：
新旧矛盾标记失效（非破坏式、保留血缘），近重复记忆融合压缩，
过期/低价值记忆降权或归档。在线（hot path）/离线（background）
双通道由控制层调度，本算子只定义演进动作本身。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from common.factory.factory import Factory
from common.type_def import MemoryUnit

from .base import ConstructionOperator


class EvolveMode(str, Enum):
    """演进阶段（对应记忆接口 ``evolve(scope, mode)`` 的 mode）。

    仅含**记忆内容**的演进阶段；索引维护不在此列——它随数据面操作
    （write/update/delete）由 IndexBuilder 增量跟进（build/update/remove），
    从真源全量重建则走 IndexBuilder.rebuild() 维护路径，均不作为 evolve 模式。
    """

    EXTRACT = "extract"
    ASSOCIATE = "associate"
    CONSOLIDATE = "consolidate"
    FORGET = "forget"


@dataclass
class EvolveResult:
    """一次演进的产出：新增/更新/被取代/被遗忘的记忆单元 id。"""

    created_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)
    superseded_ids: list[str] = field(default_factory=list)
    forgotten_ids: list[str] = field(default_factory=list)


class EvolverProducer(Factory):
    """Evolver 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``evolver_impl`` 下以 ``@EvolverProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "evolver"


class Evolver(ConstructionOperator):
    @abstractmethod
    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        """对一批记忆单元执行指定阶段的演进，返回变更结果。"""
