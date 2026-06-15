"""PolicyManager — 运行时可变策略（架构 §13.4 的 admin 落点）。

配置分两类：不可变/重型配置（真源形态、后端选型）在实例初始化时由
``src/config`` 确定；可变策略（启停某索引、检索重排开关、切换演进
模式等）支持运行时查询与调整——本算子管后者。策略变更产生审计事件。
"""

from __future__ import annotations

from abc import abstractmethod

from .base import ControlOperator


class PolicyManager(ControlOperator):
    @abstractmethod
    def get(self, key: str) -> str:
        """读取一项运行时策略的当前值。"""

    @abstractmethod
    def set(self, key: str, value: str) -> None:
        """调整一项运行时策略（仅限可变策略；键未知或试图改不可变配置时抛
        :class:`~common.errors.PolicyError`）。"""

    @abstractmethod
    def all(self) -> dict[str, str]:
        """列出全部运行时策略及当前值。"""
