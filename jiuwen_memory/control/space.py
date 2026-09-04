# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SpaceManager — space 管理面接口。

space 是 ``org`` 下的逻辑隔离单元，承担多租户 access/storage/governance
边界。API 层负责鉴权与审计，本算子负责 space 元数据、策略、成员、用量和
offboarding 状态管理。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope

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
    def begin_delete(self, org: str, space: str) -> SpaceInfo:
        """删除流程内部把 space 标为 ``DELETING``，阻断新写；对已是该状态幂等。"""

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
    def spaces_for(self, actor: Scope, org: str) -> tuple[str, ...]:
        """``actor`` 在 ``org`` 下相关的空间名，按字典序去重排序。

        与 :meth:`list` 的区别是方向：``list`` 按 org 枚举全部空间，本方法按主体反查。
        成员表按空间存放，靠遍历全部空间的成员表反查是全库扫描，因此实现须另建一份按
        主体组织的索引，并在归属登记与成员记录的每次增删处同步维护它。

        **超集语义**：允许多给，不允许遗漏。多给的部分由调用方逐空间判权裁决；遗漏则
        表现为空间对该主体不可见且无错误信号。写入次序因此是「主数据先、索引后」，删除
        次序相反。

        返回值有序是契约的一部分：调用方按序取前 N 项做上限截断，顺序不稳定即被截掉的
        空间随调用漂移。

        本方法与 :meth:`list` 同为裸算子，不含鉴权——鉴权在 API 层的逐空间判定循环里。

        ``actor`` 两维皆非空时按并集处置：用户维与代理维各自相关的空间全部返回，不取交集。
        索引只负责不遗漏，收窄由调用方的逐空间判定完成。单维主体的约束在写入侧——成员
        记录与归属登记的主体须恰好一维非空，见 :meth:`add_member`。
        """

    @abstractmethod
    def add_member(self, org: str, space: str, member: SpaceMember) -> None:
        """添加或更新 space 成员角色。

        ``member.scope`` 的 ``user`` 与 ``agent`` 至多一维非空，两维皆非空报
        :class:`~common.errors.ValidationError`：主体反查按单维组织，双维记录在两维上
        同时命中，「两维各自约束」的语义随之消失。两维皆空表示组织内全体。
        """

    @abstractmethod
    def remove_member(self, org: str, space: str, member: Scope) -> None:
        """移除 space 成员。"""
