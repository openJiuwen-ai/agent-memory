"""MembershipResolver — 空间授权事实的一次读取与主体反查（F07）。

一次判定要两类事实：空间自身的（元数据、状态、归属登记、成员记录）与主体相关的
（命中的显式授权）。两类的持有方不同——前者归本算子，后者归安全层的授权记录存储，
由判定实现自行查询。

**本算子不进判定实现的构造依赖。** 判定实现（PDP）不访问存储，事实由鉴权点（PEP）
取一次、随资源描述对象传入。两条理由：

- 构造期环：具名实例缓存在构造**完成后**才写入，互为依赖的两侧都取不到对方的半成品；
- 一次调用内多次判定各读一次事实，与「事实一次读取、全链路复用」相悖。

依赖方向因此是单向的：鉴权点 → 本算子 → SpaceManager。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope

from .base import ControlOperator
from .types import SpaceFacts


class MembershipProducer(Factory):
    """MembershipResolver 的注册式工厂。"""

    TOP_NAME = "membership"


class MembershipResolver(ControlOperator):
    """空间授权事实的读取算子。"""

    @abstractmethod
    def facts(self, org: str, space: str) -> SpaceFacts:
        """取回该空间的元数据与成员记录（已滤除过期记录）。

        不收调用方身份、不返回授权记录：两项都是防止本算子重新变成判定实现的依赖。
        后端不可用时抛 :class:`~common.errors.BackendError`，调用方按拒绝处理，
        不沿用过期结果——沿用即为放行方向的失效。
        """

    @abstractmethod
    def spaces_for(self, actor: Scope, org: str) -> tuple[str, ...]:
        """按主体反查相关空间；超集契约——不遗漏、允许多给，权限由逐空间判定裁决。

        返回值按空间名字典序去重排序：多路桶合并的天然结果是集合、顺序不定，而候选集的
        上限截断按序取前 N 项，顺序不稳定即截掉的空间随调用漂移。
        """

    @abstractmethod
    def invalidate(self, org: str, space: str | None = None) -> None:
        """使缓存的空间事实失效；成员与策略写入路径提交后由 API 层调用。

        ``space`` 为 ``None`` 即失效该 org 下的全部缓存项。
        """
