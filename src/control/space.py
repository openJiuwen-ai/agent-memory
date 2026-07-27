"""SpaceManager — space 管理面接口。

space 是 ``org`` 下的逻辑隔离单元，承担多租户 access/storage/governance
边界。API 层负责鉴权与审计，本算子负责 space 元数据、策略、成员、用量和
offboarding 状态管理。
"""

from __future__ import annotations

from abc import abstractmethod

from common.factory.factory import Factory
from common.type_def import Scope

from .base import ControlOperator
from .types import (
    SpaceDeleteResult,
    SpaceInfo,
    SpaceMember,
    SpacePatch,
    SpacePolicy,
    SpaceSpec,
    SpaceStatus,
    SpaceUsage,
)


class SpaceProducer(Factory):
    """SpaceManager 的注册式工厂。"""

    TOP_NAME = "space"


class SpaceManager(ControlOperator):
    """space 管理面算子。"""

    @abstractmethod
    def create(self, spec: SpaceSpec) -> SpaceInfo:
        """创建 space。"""

    @abstractmethod
    def get(self, org: str, space: str) -> SpaceInfo:
        """读取单个 space。"""

    @abstractmethod
    def list(
        self,
        org: str,
        *,
        status: SpaceStatus | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[SpaceInfo]:
        """列出 org 下的 spaces；cursor 为实现私有的分页游标。"""

    @abstractmethod
    def update(self, org: str, space: str, patch: SpacePatch) -> SpaceInfo:
        """修改 space 元数据、状态、主体路径或策略。"""

    @abstractmethod
    def archive(self, org: str, space: str) -> SpaceInfo:
        """归档 space。"""

    @abstractmethod
    def delete(self, org: str, space: str) -> SpaceDeleteResult:
        """删除 space 管理面记录与该 space KV 真源。"""

    @abstractmethod
    def export(self, org: str, space: str, *, include_audit: bool = True) -> str:
        """提交或生成 space 导出，返回 export id。"""

    @abstractmethod
    def usage(self, org: str, space: str) -> SpaceUsage:
        """查询 space 级用量。"""

    @abstractmethod
    def get_policy(self, org: str, space: str) -> SpacePolicy:
        """读取 space 级 policy。"""

    @abstractmethod
    def set_policy(self, org: str, space: str, policy: SpacePolicy) -> SpacePolicy:
        """替换 space 级 policy。"""

    @abstractmethod
    def list_members(self, org: str, space: str) -> list[SpaceMember]:
        """列出 space 成员。"""

    @abstractmethod
    def add_member(self, org: str, space: str, member: SpaceMember) -> None:
        """添加或更新 space 成员角色。"""

    @abstractmethod
    def remove_member(self, org: str, space: str, member: Scope) -> None:
        """移除 space 成员。"""
