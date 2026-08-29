"""MemoryAPI — 记忆接口层（B 层，架构 §9）：统一 Core API，形态无关。

所有接入形态（SDK/CLI/Skill/MCP/HTTP·gRPC）最终映射到本接口；不论
真源是文档还是结构化、运行在端还是云，调用方语义一致。

本层是控制层（``jiuwen_memory/control``）的薄封装：数据面（add/search/list/get/update/
delete/evolve/admin）委托 :class:`~control.engine.MemoryEngine`，管理面查询
（任务状态、血缘/审计、跨 scope 授权）直达对应控制算子
（:class:`~control.scheduler.Scheduler` / :class:`~control.governance.Governor`
/ :class:`~control.permission.PermissionManager`）——只做参数装配与鉴权，
编排逻辑全部在 ``jiuwen_memory/control``。接口先行过渡期的授权判定仍走
``PermissionManager``；目标实现切到
:class:`~common.security.authorization.Authorizer`（F05 §Authorization）。
调用层（SDK/CLI/MCP 等）只依赖本包即可触达全部对外能力，无需 import 内核其他包。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from jiuwen_memory.common.security.types import Grant, RequestSecurityContext
from jiuwen_memory.common.type_def import (
    AuditEvent,
    Context,
    FilterClause,
    FilterExpr,
    MemoryUnit,
    MetadataValueType,
    Modality,
    Scope,
)
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control import (
    BatchWriteItem,
    BatchWriteResult,
    Channel,
    DeleteMode,
    DeleteSelector,
    JobInfo,
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
from jiuwen_memory.retrieval import DisclosureLevel, RetrievalResult


class MemoryAPI(ABC):
    """统一记忆接口（§9 语义，不含 link——关联由构建层 Associator 在演进中维护）。

    **鉴权与审计的执行点（PEP）在本层，且本层是唯一的业务 PEP**：每个涉及租户
    数据/治理的方法都收 ``scope``（操作的目标范围 target）与 ``security``
    （本次请求的安全上下文）。接口先行过渡期从 ``security.auth.actor`` 取身份，
    仍调用 ``PermissionManager``；目标实现再从真源构造
    :class:`~common.security.types.ResourceDescriptor`、派生
    :class:`~common.security.types.AuthorizationEnvironment` 并调用
    :class:`~common.security.authorization.Authorizer`。不通过即抛
    :class:`~common.errors.PermissionDeniedError`（适用于下列所有方法，各方法不再
    重复说明）；通过后才委托 :class:`~control.engine.MemoryEngine`，且仅透传已鉴权
    的 target ``scope`` 与业务参数（调用方身份不下沉，下游信任 target）；同时在本层
    落带 actor 与稳定决策标识的入口审计事件。

    ``security`` 是本层的**唯一显式安全输入**：除 ``check_write`` 为兼容旧第二位置
    参数外均为必填 keyword-only。调用方身份只来自 ``security.auth.actor``，业务
    payload 中不存在 ``identity`` /
    ``actor_*`` / ``role`` / ``acting_user`` 之类的身份声明（F05
    §RequestSecurityContext）。target 仍由业务参数表达，不与 actor 合并——二者
    同为 Scope 时若合并，「读自己的」和「读别人的」在签名上就分不出来了。
    """

    @abstractmethod
    def add(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        """同步写入记忆；阻塞至 hot path 完成并返回本次插入的记忆单元。

        条目的可读范围由所在空间的权限决定，写入侧不另设条目级的可见性声明（F07）。

        ``scope`` 必填，它就是落点。落点也可以交给归属判定选择：在参数袋里放
        ``system_metadata["coords"]`` 即请求判定，此时 ``scope.space`` 不参与落点计算，
        真实落点由返回的记忆单元携带。表中「判定表非空」指配置的 ``router`` 命名空间下声明了
        记忆类别，未声明即为空表、本节全部变更不可达：

        | 判定表非空 | ``coords`` 键 | ``scope.space`` | 落点 |
        |---|---|---|---|
        | 否 | 任意 | 任意 | 就是 ``scope`` |
        | 是 | 有 | 任意 | 由判定算子在候选空间集合内选 |
        | 是 | 无 | 非空 | 就是 ``scope`` |
        | 是 | 无 | 空 | 拒绝：既未指定落点，也未请求判定 |

        判据取 ``coords`` 键的有无，不取 ``scope`` 的取值形态，理由见 F07「接口契约」；
        取值为 ``{}`` 是合法的判定请求，表示「请判定，但本次没有业务坐标」。

        两条路径都校验入参 ``scope`` 的主体维——归属不由调用方声明，与身份不符即拒绝，判定
        路径另校验 ``org``。判定标签键按落盘不变量恒写，直写路径上取值一律空串。

        **归属坐标经参数袋传入，不占形参**：写入侧 ``system_metadata["coords"]``，检索侧
        ``Context.extensions["coords"]``，取值为 ``dict[str, str]``，表达这次交互发生在
        什么上下文。``user`` / ``agent`` / ``session`` 三项以身份为准、不接受覆盖，其余键由
        部署的 ``coord_entities`` 声明。该键在本层即被取出，不落盘、不进鉴权入参、不随
        options 透传给自定义检索模块；检索侧它只作收窄谓词，跨空间检索另由 ``extensions["spaces"]``
        触发（见 :meth:`search`），两者互不相干。

        取值不受 ``MetadataValueType`` 约束（该联合类型不含 ``dict``），与 ``route_ctx``
        承载判定上下文对象同例；判型改在运行期做，键名拼写错误无从覆盖——内核区分不了
        「拼错」与「本次不带该坐标」，失效方向是该维不收窄。
        """

    @abstractmethod
    async def add_async(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        """异步写入记忆；语义与 :meth:`add` 一致并直通引擎协程。"""

    @abstractmethod
    def batch_add(
        self,
        items: list[BatchWriteItem],
        scope: Scope | None = None,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
        stream_id: str = "",
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        """同步批量写入；结果按输入顺序逐项对齐。

        ``scope`` 是批级缺省值，逐项 ``BatchWriteItem.scope`` 为 ``None`` 时沿用它；两级都
        不给即该项没有落点，除非批级参数袋请求了判定（见 :meth:`add`）。判定请求是批级的，
        逐项参数袋携带 ``coords`` 即拒绝——整批共用一次判定上下文。
        """

    @abstractmethod
    async def batch_add_async(
        self,
        items: list[BatchWriteItem],
        scope: Scope | None = None,
        source: Modality = Modality.TEXT,
        *,
        security: RequestSecurityContext,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
        stream_id: str = "",
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        """异步批量写入；语义与 :meth:`batch_add` 一致。"""

    @abstractmethod
    def search(
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
        """执行混合检索；由 ``context`` 指定 scope、透传 options 与披露预算。

        ``context.extensions`` 带 ``spaces`` 键时转为跨空间检索（F07「多空间读写」），
        取值为 ``list[str]``：**判据取键的有无，不取取值形态**——键不在即单空间检索，
        行为与本特性之前一字不差；键在即跨空间，空列表表示「调用方可读的全部空间」（由
        主体反查索引给出），非空即显式候选集。该键在本层即被取出，不随 options 透传给
        自定义检索模块。取值为 ``None`` 按非法拒绝，不当作空列表——网关把未填字段序列化
        成 ``null`` 时若按空列表处置，一次本意为单空间的检索会静默扩到全部可读空间。

        跨空间不是新的检索算法，是在单空间召回之上套的一层编排：候选空间 → 逐空间判权
        → 按上界分配取数 → 逐空间召回 → 跨空间按内容去重、截到 ``top_k``。两族谓词与
        召回复用同一份实现。不另设入口的理由见 F07「多空间读写」。

        跨空间形态下的三处差异：

        - ``context.scope``：只取 ``org`` 维定组织边界，空间维由候选集给出、传了不生效。
        - 无权的候选空间：逐个剔除并记入 ``RetrievalResult.errors``，不使整次调用失败；
          候选集非空而一个都读不到时抛 ``PermissionDeniedError``，与单空间路径同一处置。
        - 时延：随候选空间数线性增长。召回按并发写就，但引擎侧 ``recall`` 当前是同步实现、
          实际顺序执行，候选集上限（``space.fanout_limit``）因而同时是时延上界的约束项。

        取同步形态：多空间召回在实现内部完成，不向调用方暴露异步契约。
        """

    @abstractmethod
    def list(
        self,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, Any] | None = None,
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
        区间含 ``as_of`` 的那一版。不存在时抛 :class:`~common.errors.NotFoundError`。
        """

    @abstractmethod
    def update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch, *, security: RequestSecurityContext
    ) -> MemoryUnit:
        """修正记忆：``scope`` 为目标范围、``security`` 为本次请求的安全上下文（本层据二者
        鉴权 UPDATE）。版本语义由 ``patch.mode`` 决定——``SUPERSEDE``（默认、
        非破坏式）新建新 id 版本、旧版标记 superseded、新版 ``supersedes`` 指向
        旧 id；``OVERWRITE`` 原地覆写沿用同 id、旧内容仅留审计。返回结果记忆
        单元（SUPERSEDE 为新 id，OVERWRITE 为原 id）。
        """

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
        id，状态用 :meth:`job_status` 查询。索引维护不在此——它随 add/update/delete
        自动跟进。
        """

    @abstractmethod
    def check_write(
        self,
        scope: Scope,
        security: RequestSecurityContext,
        *,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
    ) -> None:
        """Pre-flight WRITE 鉴权，不落盘。用于长耗时摄入任务入队前拒绝无权限请求，
        避免 DoS（队列被无权限请求占满）。后台实际写入仍保留一次鉴权作防御层。
        """

    @abstractmethod
    def job_status(
        self,
        job_id: str,
        *,
        security: RequestSecurityContext,
        scope: Scope | None = None,
    ) -> JobInfo:
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
        本层据 ``security`` 鉴权 ``READ_AUDIT``。
        """

    # -- 跨 scope 授权（委托 PermissionManager，架构 §3.2） ------------------- #

    @abstractmethod
    def grant(self, grant: Grant, *, security: RequestSecurityContext) -> Grant:
        """
        新增一条跨 scope 授权（共享池等）；本层据 ``security`` 鉴权 SHARE
        （须有权再授权 ``grant.grantor`` 范围）。返回值携带该授权的 ``grant_id``，
        供后续精确撤销。

        接口先行过渡期：``GrantStore`` 未实装，服务端尚不生成 ``grant_id``，
        返回值原样回传入参（见 F05-security-api-contracts §5.4）。
        """

    @abstractmethod
    def revoke(self, grant: Grant, *, security: RequestSecurityContext) -> None:
        """
        回收一条授权（幂等）；本层据 ``security`` 鉴权 ``REVOKE_SHARE``。
        目标语义是按 ``grant.grant_id`` 精确定位。

        接口先行过渡期：``GrantStore`` 未实装，实际仍按
        ``grantor + grantee + action`` 条件撤销，``grant_id`` 不参与定位
        （见 F05-security-api-contracts §5.4）。
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
