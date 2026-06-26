"""MemoryAPI — 记忆接口层（B 层，架构 §9）：统一 Core API，形态无关。

所有接入形态（SDK/CLI/Skill/MCP/HTTP·gRPC）最终映射到本接口；不论
真源是文档还是结构化、运行在端还是云，调用方语义一致。

本层是控制层（``src/control``）的薄封装：数据面（write/recall/get/update/
delete/evolve/admin）委托 :class:`~control.engine.MemoryEngine`，管理面查询
（任务状态、血缘/审计、跨 scope 授权）直达对应控制算子
（:class:`~control.scheduler.Scheduler` / :class:`~control.governance.Governor`
/ :class:`~control.permission.PermissionManager`）——只做参数装配与鉴权，
编排逻辑全部在 ``src/control``。调用层（SDK/CLI/MCP 等）只依赖本包即可触达
全部对外能力，无需 import 内核其他包。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from common.type_def import (
    AuditEvent,
    Context,
    FilterClause,
    MemoryUnit,
    Modality,
    Scope,
)
from construction import EvolveMode
from control import Channel, DeleteSelector, Grant, JobInfo, MemoryPatch
from retrieval import DisclosureLevel, RetrievalResult


class MemoryAPI(ABC):
    """统一记忆接口（§9 语义，不含 link——关联由构建层 Associator 在演进中维护）。

    **鉴权与审计的执行点（PEP）在本层**：每个涉及租户数据/治理的方法都收
    ``scope``（操作的目标范围 target）与 ``identity``（调用方身份）两个 Scope。
    本层先 ``PermissionManager.check(identity, scope, action)``，不通过即抛
    :class:`~common.errors.PermissionDeniedError`（适用于下列所有方法，各方法
    不再重复说明）；通过后才委托 :class:`~control.engine.MemoryEngine`，且仅
    透传已鉴权的 target ``scope``（identity 不下沉，下游信任 target）；同时在本层
    落带 identity 的入口审计事件。

    ``identity`` 一律为**必填 keyword-only** 参数：它与 target ``scope`` 同为
    Scope 类型，强制具名传入可杜绝二者位置传反导致的越权。
    """

    @abstractmethod
    def write(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        identity: Scope,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, str] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        """同步写入记忆：``scope`` 为写入目标范围、``identity`` 为调用方身份
        （本层据二者鉴权 WRITE 并落入口审计）；``content`` 文本/结构投影 +
        可选 ``assets`` 原模态资产引用；阻塞至 hot path 完成（落盘 + 轻量
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
        identity: Scope,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, str] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        """异步（协程）写入记忆：语义与 :meth:`write` 一致（同样以
        ``scope``/``identity`` 鉴权），同样返回插入的记忆单元；直通引擎的异步
        write，供事件循环/高并发接入形态（HTTP/MCP）非阻塞调用。"""

    @abstractmethod
    def recall(
        self,
        query: str,
        context: Context,
        *,
        identity: Scope,
        filters: list[FilterClause] | None = None,
        as_of: datetime | None = None,
        top_k: int = 10,
        disclosure: DisclosureLevel = DisclosureLevel.L0,
        with_trajectory: bool = False,
    ) -> RetrievalResult:
        """混合检索召回。``context`` 携带检索目标范围 ``context.scope`` 与调用级透传配置
        ``context.extensions``（**不被内核核心逻辑解释**，值为传输安全的 ``str``），
        ``identity`` 为调用方身份（本层据 scope/identity 鉴权 READ）。边界处把 Context
        拆开：scope 作独立轴穿透；``extensions`` 中约定 key ``max_tokens``（自适应披露预算）
        被解析为 int 写入 ``RetrievalQuery`` 由披露阶段消费，其余 ``extensions`` 写入调用级
        options、顺 parser 进 ``ParsedQuery`` 供自定义检索模块按约定 key 读取；Context
        本身不下沉。``filters`` 标签/元数据前置过滤（结构化
        :class:`~common.type_def.FilterClause` 列表，支持等值/集合/范围，AND 组合），
        ``as_of`` 时间点回溯（双时间模型），``disclosure`` 渐进式披露层级，
        ``with_trajectory`` 返回检索轨迹。"""

    @abstractmethod
    def get(
        self, unit_id: str, scope: Scope, *, identity: Scope, as_of: datetime | None = None
    ) -> MemoryUnit:
        """按 id 读取记忆单元；``scope`` 为目标范围、``identity`` 为调用方身份
        （本层据二者 check READ 后才下发，不读数据即可判权）。``as_of`` 为空时
        返回该 id 对应的那一条；非空时沿 ``supersedes`` 版本链回溯，返回 valid
        区间含 ``as_of`` 的那一版。不存在时抛 :class:`~common.errors.NotFoundError`。"""

    @abstractmethod
    def update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch, *, identity: Scope
    ) -> MemoryUnit:
        """修正记忆：``scope`` 为目标范围、``identity`` 为调用方身份（本层据二者
        鉴权 UPDATE）。版本语义由 ``patch.mode`` 决定——``SUPERSEDE``（默认、
        非破坏式）新建新 id 版本、旧版标记 superseded、新版 ``supersedes`` 指向
        旧 id；``OVERWRITE`` 原地覆写沿用同 id、旧内容仅留审计。返回结果记忆
        单元（SUPERSEDE 为新 id，OVERWRITE 为原 id）。"""

    @abstractmethod
    def delete(self, selector: DeleteSelector, *, identity: Scope) -> list[str]:
        """
        删除：按选择器遗忘/归档/降权（非破坏式、可审计、可恢复策略）；
        ``identity`` 为调用方身份，本层据 ``selector.scope`` 鉴权 DELETE；返回
        命中的记忆单元 id。
        """

    @abstractmethod
    def evolve(
        self,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel = Channel.BACKGROUND,
        *,
        identity: Scope,
    ) -> str:
        """触发演进（extract/associate/consolidate/forget）：``scope`` 为演进
        目标范围、``identity`` 为调用方身份（本层据二者鉴权）；返回任务 id，状态
        用 :meth:`job_status` 查询。索引维护不在此——它随 write/update/delete
        自动跟进。"""

    @abstractmethod
    def job_status(self, job_id: str, *, identity: Scope) -> JobInfo:
        """
        查询演进任务状态（委托 Scheduler）；``identity`` 为调用方身份，本层
        据其鉴权（仅可查自身/已授权范围的任务）。
        """

    @abstractmethod
    def job_cancel(self, job_id: str, *, identity: Scope) -> None:
        """
        取消尚未完成的演进任务（幂等，委托 Scheduler）；``identity`` 为调用
        方身份，本层据其鉴权。
        """

    @abstractmethod
    def admin_get(self, key: str, *, identity: Scope) -> str:
        """
        admin：读取一项运行时策略的当前值；``identity`` 为调用方身份（本层
        据其做管理面鉴权）。
        """

    @abstractmethod
    def admin_set(self, key: str, value: str, *, identity: Scope) -> None:
        """
        admin：调整一项运行时策略（启停索引、检索/演进开关等；键未知或
        不可变配置抛 :class:`~common.errors.PolicyError`）；``identity`` 为调用方
        身份，本层据其鉴权并落审计。
        """

    @abstractmethod
    def admin_all(self, *, identity: Scope) -> dict[str, str]:
        """
        admin：列出全部运行时策略及当前值；``identity`` 为调用方身份（本层
        据其做管理面鉴权）。
        """

    # -- 治理（委托 Governor，架构 §12 的「看」侧） --------------------------- #

    @abstractmethod
    def inspect(self, unit_ids: list[str], scope: Scope, *, identity: Scope) -> list[MemoryUnit]:
        """
        检视：读取记忆单元的完整内容与治理字段（含已失效版本）。

        ``scope`` 为目标范围，``identity`` 为调用方身份，本层据二者鉴权。
        """

    @abstractmethod
    def trace(self, unit_id: str, scope: Scope, *, identity: Scope) -> list[MemoryUnit]:
        """
        血缘回溯：沿 provenance 向上追溯该记忆的演进来源链；``scope`` 为
        目标范围、``identity`` 为调用方身份，本层据二者鉴权。
        """

    @abstractmethod
    def audit(
        self, filters: dict[str, str], *, identity: Scope, limit: int = 100
    ) -> list[AuditEvent]:
        """审计查询：按条件（actor/action/layer/时间段等）检索审计留痕；
        ``identity`` 为调用方身份，本层据其做治理鉴权。"""

    # -- 跨 scope 授权（委托 PermissionManager，架构 §3.2） ------------------- #

    @abstractmethod
    def grant(self, grant: Grant, *, identity: Scope) -> None:
        """
        新增一条跨 scope 授权（共享池等）；``identity`` 为调用方身份，本层据
        其鉴权 SHARE（须有权再授权 ``grant.grantor`` 范围）。
        """

    @abstractmethod
    def revoke(self, grant: Grant, *, identity: Scope) -> None:
        """
        回收一条授权（幂等）；``identity`` 为调用方身份，本层据其鉴权。匹配
        哪条既有授权由具体实现定义。
        """
