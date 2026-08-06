"""MemoryAPI — 记忆接口层（B 层，架构 §9）：统一 Core API，形态无关。

所有接入形态（SDK/CLI/Skill/MCP/HTTP·gRPC）最终映射到本接口；不论
真源是文档还是结构化、运行在端还是云，调用方语义一致。

本层是控制层（``src/control``）的薄封装：数据面（write/recall/list/get/update/
delete/evolve/admin）委托 :class:`~control.engine.MemoryEngine`，管理面查询
（任务状态、血缘/审计、跨 scope 授权）直达对应控制算子
（:class:`~control.scheduler.Scheduler` / :class:`~control.governance.Governor`
/ :class:`~control.permission.PermissionManager`）——只做参数装配与鉴权，
编排逻辑全部在 ``src/control``；授权判定归
:class:`~common.security.authorization.Authorizer`（F05 §Authorization）。
调用层（SDK/CLI/MCP 等）只依赖本包即可触达全部对外能力，无需 import 内核其他包。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from common.security.types import RequestSecurityContext
from common.type_def import (
    AuditEvent,
    Context,
    FilterClause,
    FilterExpr,
    MemoryUnit,
    Modality,
    Scope,
)
from construction import EvolveMode
from control import (
    Channel,
    DeleteMode,
    DeleteSelector,
    Grant,
    JobInfo,
    BatchWriteItem,
    BatchWriteResult,
    MemoryListResult,
    MemoryPatch,
    SpaceDeleteResult,
    SpaceInfo,
    SpaceMember,
    SpacePatch,
    SpacePolicy,
    SpaceSpec,
    SpaceStatus,
    SpaceUsage,
)
from retrieval import DisclosureLevel, RetrievalResult


class MemoryAPI(ABC):
    """统一记忆接口（§9 语义，不含 link——关联由构建层 Associator 在演进中维护）。

    **鉴权与审计的执行点（PEP）在本层，且本层是唯一的业务 PEP**：每个涉及租户
    数据/治理的方法都收 ``scope``（操作的目标范围 target）与 ``security``
    （本次请求的安全上下文）。本层把 verb 映射为封闭的
    :class:`~common.security.types.Action`、从真源构造
    :class:`~common.security.types.ResourceDescriptor`、派生
    :class:`~common.security.types.AuthorizationEnvironment`，再调用
    :class:`~common.security.authorization.Authorizer`；不通过即抛
    :class:`~common.errors.PermissionDeniedError`（适用于下列所有方法，各方法不再
    重复说明）；通过后才委托 :class:`~control.engine.MemoryEngine`，且仅透传已鉴权
    的 target ``scope`` 与业务参数（调用方身份不下沉，下游信任 target）；同时在本层
    落带 actor 与稳定决策标识的入口审计事件。

    ``security`` 一律为**必填 keyword-only** 参数，且是本层的**唯一显式安全输入**：
    调用方身份只来自 ``security.auth.actor``，业务 payload 中不存在 ``identity`` /
    ``actor_*`` / ``role`` / ``acting_user`` 之类的身份声明（F05
    §RequestSecurityContext）。target 仍由业务参数表达，不与 actor 合并——二者
    同为 Scope 时若合并，「读自己的」和「读别人的」在签名上就分不出来了。
    """

    @abstractmethod
    def write(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        """同步写入记忆：``scope`` 为写入目标范围、``security`` 为本次请求的
        安全上下文（本层据 actor 与 target 鉴权 WRITE 并落入口审计）；``content``
        文本/结构投影 + 可选 ``assets`` 原模态资产引用；阻塞至 hot path 完成（落盘 + 轻量
        索引），返回本次插入的全部记忆单元（规约/切分可产生多条）；重演进
        由 background 通道异步进行。实现上桥接引擎的异步 write（如
        ``asyncio.run``），供 CLI/脚本等同步形态使用。"""

    @abstractmethod
    async def write_async(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        """异步（协程）写入记忆：语义与 :meth:`write` 一致（同样以
        ``scope``/``security`` 鉴权），同样返回插入的记忆单元；直通引擎的异步
        write，供事件循环/高并发接入形态（HTTP/MCP）非阻塞调用。"""

    @abstractmethod
    def batch_write(
        self,
        items: list[BatchWriteItem],
        scope: Scope | None = None,
        source: Modality = Modality.TEXT,
        *,
        identity: Scope,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
        stream_id: str = "",
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        """同步批量写入；结果按输入顺序逐项对齐。"""

    @abstractmethod
    async def batch_write_async(
        self,
        items: list[BatchWriteItem],
        scope: Scope | None = None,
        source: Modality = Modality.TEXT,
        *,
        identity: Scope,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
        stream_id: str = "",
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        """异步批量写入；语义与 :meth:`batch_write` 一致。"""

    @abstractmethod
    def recall(
        self,
        query: str,
        context: Context,
        *,
        security: RequestSecurityContext,
        filters: FilterExpr | list[FilterClause] | dict | None = None,
        as_of: datetime | None = None,
        top_k: int = 10,
        disclosure: DisclosureLevel = DisclosureLevel.L0,
        with_trajectory: bool = False,
    ) -> RetrievalResult:
        """混合检索召回。``context`` 携带检索目标范围 ``context.scope`` 与调用级透传配置
        ``context.extensions``（**不被内核核心逻辑解释**，值为传输安全的 ``str``），
        ``security`` 为本次请求的安全上下文（本层据 actor 与 ``context.scope``
        鉴权 READ）。边界处把 Context 拆开：scope 作独立轴穿透；``extensions`` 中
        约定 key ``max_tokens``（自适应披露预算）被解析为 int 写入
        ``RetrievalQuery`` 由披露阶段消费，其余 ``extensions`` 写入调用级
        options、顺 parser 进 ``ParsedQuery`` 供自定义检索模块按约定 key 读取；Context
        本身不下沉。``filters`` 在本边界兼容旧 list、单 clause 和 dict DSL，并立即
        规范化为支持 AND/OR/NOT 的 :class:`~common.type_def.FilterExpr`，
        ``as_of`` 时间点回溯（双时间模型），``disclosure`` 渐进式披露层级，
        ``with_trajectory`` 返回检索轨迹。"""

    @abstractmethod
    def list(
        self,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, str] | None = None,
        filters: FilterExpr | list[FilterClause] | dict | None = None,
    ) -> MemoryListResult:
        """列出目标 ``scope`` 下已建索引的记忆单元。

        语义参考 mem1.0 ``list_memories``：支持 ``offset``/``limit`` 分页与
        ``memory_types`` 类型过滤，``extensions`` 透传自定义参数，``filters``
        支持结构化过滤。只返回 ``/memory/`` 真源记录，不包含
        ``/messages/`` 下的 infer 原文缓存。``security`` 为本次请求的安全上下文，
        本层据 ``scope`` 鉴权 READ 后委托 Engine。返回当前页 items 和分页前匹配总数 count。
        """

    @abstractmethod
    def get(
        self,
        unit_id: str,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        as_of: datetime | None = None,
    ) -> MemoryUnit:
        """按 id 读取记忆单元；``scope`` 为目标范围、``security`` 为本次请求的安全上下文
        （本层据二者鉴权 READ 后才下发，不读数据即可判权）。``as_of`` 为空时
        返回该 id 对应的那一条；非空时沿 ``supersedes`` 版本链回溯，返回 valid
        区间含 ``as_of`` 的那一版。不存在时抛 :class:`~common.errors.NotFoundError`。"""

    @abstractmethod
    def update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch, *, security: RequestSecurityContext
    ) -> MemoryUnit:
        """修正记忆：``scope`` 为目标范围、``security`` 为本次请求的安全上下文（本层据二者
        鉴权 UPDATE）。版本语义由 ``patch.mode`` 决定——``SUPERSEDE``（默认、
        非破坏式）新建新 id 版本、旧版标记 superseded、新版 ``supersedes`` 指向
        旧 id；``OVERWRITE`` 原地覆写沿用同 id、旧内容仅留审计。返回结果记忆
        单元（SUPERSEDE 为新 id，OVERWRITE 为原 id）。"""

    @abstractmethod
    def delete(self, selector: DeleteSelector, *, security: RequestSecurityContext) -> list[str]:
        """
        删除：按选择器遗忘/归档/降权（非破坏式、可审计、可恢复策略）；
        ``security`` 为本次请求的安全上下文，本层据 ``selector.scope`` 鉴权
        DELETE；返回命中的记忆单元 id。
        """

    @abstractmethod
    def evolve(
        self,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel = Channel.BACKGROUND,
        *,
        security: RequestSecurityContext,
    ) -> str:
        """触发演进（extract/associate/consolidate/forget）：``scope`` 为演进
        目标范围、``security`` 为本次请求的安全上下文（本层据二者鉴权）；返回任务
        id，状态用 :meth:`job_status` 查询。索引维护不在此——它随 write/update/delete
        自动跟进。"""

    @abstractmethod
    def job_status(self, job_id: str, *, security: RequestSecurityContext) -> JobInfo:
        """
        查询演进任务状态（委托 Scheduler）；``security`` 为本次请求的安全上下文，本层
        据其鉴权（仅可查自身/已授权范围的任务）。
        """

    @abstractmethod
    def job_cancel(self, job_id: str, *, security: RequestSecurityContext) -> None:
        """
        取消尚未完成的演进任务（幂等，委托 Scheduler）；``security`` 为本次请求的
        安全上下文，本层据其鉴权。
        """

    @abstractmethod
    def admin_get(self, key: str, *, security: RequestSecurityContext) -> str:
        """
        admin：读取一项运行时策略的当前值；本层据 ``security`` 做管理面鉴权
        （``MANAGE_POLICY``）。
        """

    @abstractmethod
    def admin_set(self, key: str, value: str, *, security: RequestSecurityContext) -> None:
        """
        admin：调整一项运行时策略（启停索引、检索/演进开关等；键未知或
        不可变配置抛 :class:`~common.errors.PolicyError`）；本层据 ``security`` 做管理面鉴权
        （``MANAGE_POLICY``）并落审计。
        """

    @abstractmethod
    def admin_all(self, *, security: RequestSecurityContext) -> dict[str, str]:
        """
        admin：列出全部运行时策略及当前值；本层据 ``security`` 做管理面鉴权
        （``MANAGE_POLICY``）。
        """

    # -- 治理（委托 Governor，架构 §12 的「看」侧） --------------------------- #

    @abstractmethod
    def inspect(
        self, unit_ids: list[str], scope: Scope, *, security: RequestSecurityContext
    ) -> list[MemoryUnit]:
        """
        检视：读取记忆单元的完整内容与治理字段（含已失效版本）。

        ``scope`` 为目标范围，``security`` 为本次请求的安全上下文，本层据二者鉴权。
        """

    @abstractmethod
    def trace(
        self, unit_id: str, scope: Scope, *, security: RequestSecurityContext
    ) -> list[MemoryUnit]:
        """
        血缘回溯：沿 provenance 向上追溯该记忆的演进来源链；``scope`` 为
        目标范围、``security`` 为本次请求的安全上下文，本层据二者鉴权。
        """

    @abstractmethod
    def audit(
        self, filters: dict[str, str], *, security: RequestSecurityContext, limit: int = 100
    ) -> list[AuditEvent]:
        """审计查询：按条件（actor/action/layer/时间段等）检索审计留痕；
        本层据 ``security`` 鉴权 ``READ_AUDIT``。"""

    # -- 跨 scope 授权（写入 Authorizer 读取的 GrantStore） ------------------- #

    @abstractmethod
    def grant(self, grant: Grant, *, security: RequestSecurityContext) -> None:
        """
        新增一条跨 scope 授权（共享池等）；本层据 ``security`` 鉴权 SHARE
        （须有权再授权 ``grant.grantor`` 范围），通过后写入 Authorizer 判定读取的
        ``GrantStore``（经 ``authorizer.management_grant_store()`` 共享同一真源）。
        """

    @abstractmethod
    def revoke(self, grant: Grant, *, security: RequestSecurityContext) -> None:
        """
        回收一条授权（幂等）；本层据 ``security`` 鉴权 ``REVOKE_SHARE``，按
        (grantor, grantee, action) 选择子定位真源记录、按 ``grant_id`` 撤销。
        """

    # -- Space 管理（委托 SpaceManager） ------------------------------------ #

    @abstractmethod
    def create_space(self, spec: SpaceSpec, *, security: RequestSecurityContext) -> SpaceInfo:
        """创建 space，并写入主体路径、策略、状态与 metadata。"""

    @abstractmethod
    def get_space(self, org: str, space: str, *, security: RequestSecurityContext) -> SpaceInfo:
        """读取单个 space 的基础信息与策略。"""

    @abstractmethod
    def list_spaces(
        self,
        org: str,
        *,
        security: RequestSecurityContext,
        status: SpaceStatus | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[SpaceInfo]:
        """列出 org 下调用方可见的 spaces。"""

    @abstractmethod
    def update_space(
        self, org: str, space: str, patch: SpacePatch, *, security: RequestSecurityContext
    ) -> SpaceInfo:
        """修改 space display name、metadata、policy 或状态。"""

    @abstractmethod
    def archive_space(self, org: str, space: str, *, security: RequestSecurityContext) -> SpaceInfo:
        """归档 space，保留读取、导出与审计能力。"""

    @abstractmethod
    def delete_space(
        self,
        org: str,
        space: str,
        *,
        security: RequestSecurityContext,
        mode: DeleteMode = DeleteMode.PURGE,
    ) -> SpaceDeleteResult:
        """删除 space 真源与可重建索引；当前实现只支持 PURGE。"""

    @abstractmethod
    def export_space(
        self,
        org: str,
        space: str,
        *,
        security: RequestSecurityContext,
        include_audit: bool = True,
    ) -> str:
        """提交 space 导出，返回 export id。"""

    @abstractmethod
    def space_usage(self, org: str, space: str, *, security: RequestSecurityContext) -> SpaceUsage:
        """查询 space 级用量。"""

    @abstractmethod
    def get_space_policy(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> SpacePolicy:
        """读取 space 级 policy。"""

    @abstractmethod
    def set_space_policy(
        self, org: str, space: str, policy: SpacePolicy, *, security: RequestSecurityContext
    ) -> SpacePolicy:
        """替换 space 级 policy。"""

    @abstractmethod
    def list_space_members(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> list[SpaceMember]:
        """列出 space 成员。"""

    @abstractmethod
    def add_space_member(
        self, org: str, space: str, member: SpaceMember, *, security: RequestSecurityContext
    ) -> None:
        """添加或更新 space 成员角色。"""

    @abstractmethod
    def remove_space_member(
        self, org: str, space: str, member: Scope, *, security: RequestSecurityContext
    ) -> None:
        """移除 space 成员。"""
