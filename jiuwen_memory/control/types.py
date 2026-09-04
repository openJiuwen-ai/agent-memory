# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""控制层接口涉及的数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from jiuwen_memory.common.security.space_roles import (
    SpaceAuthorizationFacts,
    SpaceContentRole,
    SpaceGovernanceRole,
)
from jiuwen_memory.common.security.types import Action as Action
from jiuwen_memory.common.security.types import Grant as Grant
from jiuwen_memory.common.type_def import (
    FilterExpr,
    MemoryTier,
    MemoryUnit,
    MetadataValueType,
    Modality,
    Scope,
)


class PrincipalPath(str, Enum):
    """space 内主体归属路径。"""

    USER_AGENT = "user_agent"
    AGENT_USER = "agent_user"


class SpaceStatus(str, Enum):
    """space 生命周期状态。"""

    ACTIVE = "active"
    FROZEN = "frozen"
    ARCHIVED = "archived"
    DELETING = "deleting"
    DELETED = "deleted"


@dataclass
class SpacePolicy:
    """space 级策略。

    这些策略描述租户隔离、主体路径、保留期、配额、索引与 pipeline profile。
    默认值保持本地单租户兼容；生产多租户部署通常应把 ``require_space`` 打开。
    """

    require_space: bool = False
    principal_path: PrincipalPath = PrincipalPath.USER_AGENT
    storage_isolation_strategy: str = "metadata_filter"
    retention: dict[str, str] = field(default_factory=dict)
    quotas: dict[str, str] = field(default_factory=dict)
    index_profiles: dict[str, str] = field(default_factory=dict)
    pipeline_profiles: dict[str, str] = field(default_factory=dict)


@dataclass
class SpaceSpec:
    """创建 space 的输入。"""

    org: str = ""
    space: str = ""
    display_name: str = ""
    principal_path: PrincipalPath = PrincipalPath.USER_AGENT
    policy: SpacePolicy = field(default_factory=SpacePolicy)
    metadata: dict[str, str] = field(default_factory=dict)
    # 归属主体，由调用方填写，API 层校验同组织与主体维恰好一维非空两项。「非组织管理员
    # 不得填他人」尚未实现：判据取组织管理员角色，而组织级角色不属本特性范围
    # （F07 决策 4）。该入口回落改造前的判据，实际持有者是运维通道。
    # 三种形态：``None`` 由 API 层填入调用方身份推导值；主体维全空的 ``Scope()`` 即不
    # 登记，共享空间取此形态——其治理权由成员记录的治理轴与组织管理员的治理放行承担，
    # 本特性内无组织管理员判据，该形态的第一个成员须由运维脚本直接经 SpaceManager 写入；
    # 具名主体即替该主体登记，用于开通服务预建用户主空间。
    owner: Scope | None = None


@dataclass
class SpaceInfo:
    """space 元数据。"""

    org: str = ""
    space: str = ""
    display_name: str = ""
    status: SpaceStatus = SpaceStatus.ACTIVE
    principal_path: PrincipalPath = PrincipalPath.USER_AGENT
    policy: SpacePolicy = field(default_factory=SpacePolicy)
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime | None = None
    archived_at: datetime | None = None
    # 归属主体登记（F07）：一个没有任何成员记录的空间在判定链看来无人可放行，本字段
    # 使个体空间的本人可经归属对比通过。每项取单维（登记双维则本人直接调用时对不上）；
    # 新建恒为至多一项，列表形态只为兼容回填产生的多归属空间。
    owners: list[Scope] = field(default_factory=list)


@dataclass
class SpacePatch:
    """修改 space 的输入；仅非 None 字段生效。"""

    display_name: str | None = None
    status: SpaceStatus | None = None
    principal_path: PrincipalPath | None = None
    policy: SpacePolicy | None = None
    metadata: dict[str, str] | None = None


@dataclass
class SpaceMember:
    """space 成员与两轴角色（F07）。

    ``role`` 是改造前的单轴角色，判定不再读取，保留只为存量数据的读取兼容；
    未识别取值按最低档解析并记警告，不抛异常——拒绝解析会使存量空间整体不可用。
    """

    scope: Scope = field(default_factory=Scope)
    role: str = "member"  # 旧单轴角色，判定不再读
    # 内容轴：空间内条目的读写改删
    content_role: SpaceContentRole = SpaceContentRole.CONTRIBUTOR
    # 治理轴：成员与策略，不含条目读取权
    governance_role: SpaceGovernanceRole = SpaceGovernanceRole.NONE
    created_at: datetime | None = None
    expires_at: datetime | None = None  # 已过期的记录不参与判定


@dataclass(frozen=True)
class SpaceFacts:
    """一次调用内的空间授权事实快照，由 API 层取一次、贯穿判定与谓词生成（F07）。

    判定实现看到的是它的最小投影（``SpaceAuthorizationFacts``）：空间策略、生命周期
    状态与成员记录的时间戳不进判定，只在鉴权点使用。
    """

    org: str = ""
    space: str = ""
    info: SpaceInfo | None = None  # 空间不存在时为 None（隐式创建路径）
    members: tuple[SpaceMember, ...] = ()  # 已滤除过期记录

    @property
    def is_individual(self) -> bool:
        """成员表为空即个体空间——归属对比的前提之一、检索第一族谓词的开关。"""
        return not self.members


@dataclass
class SpaceUsage:
    """space 级用量统计。"""

    org: str = ""
    space: str = ""
    memory_count: int = 0
    message_count: int = 0
    index_count: int = 0
    storage_bytes: int = 0
    audit_count: int = 0


@dataclass
class SpaceDeleteResult:
    """space 删除结果。"""

    org: str = ""
    space: str = ""
    deleted_counts: dict[str, int] = field(default_factory=dict)
    status: SpaceStatus = SpaceStatus.DELETED
    audit_event_id: str = ""


@dataclass
class MemoryListResult:
    """List 数据面结果：当前页 MemoryUnit 与分页前匹配总数。"""

    items: list[MemoryUnit] = field(default_factory=list)
    count: int = 0


@dataclass
class SweepResult:
    """``MemoryEngine.sweep_expired`` 编排结果。

    ``swept``：索引清理与真源回写均完成的 unit id；
    ``failed``：任一步失败、真源保持原状态的 unit id——单元未流转，下轮
    sweep 会重新发现（remove 幂等），重试自愈，不静默当成功。
    """

    swept: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


@dataclass
class BatchWriteItem:
    """一条批量写入输入；API 归一化后再交给 Engine。"""

    content: str
    scope: Scope | None = None
    source: Modality | None = None
    assets: list[str] | None = None
    tags: list[str] | None = None
    system_metadata: dict[str, MetadataValueType] | None = None
    user_metadata: dict[str, MetadataValueType] | None = None
    occurred_at: datetime | None = None
    stream_id: str = ""
    sequence: int | None = None
    idempotency_key: str = ""


@dataclass
class BatchWriteOutcome:
    """批量写入的逐项结果。"""

    index: int
    item: BatchWriteItem
    units: list[MemoryUnit] = field(default_factory=list)
    error: str = ""
    error_type: str = ""


@dataclass
class BatchWriteResult:
    """与输入顺序对齐的批量写入结果。"""

    outcomes: list[BatchWriteOutcome] = field(default_factory=list)


@dataclass(frozen=True)
class PermissionContext:
    """权限判断的资源上下文。

    `PermissionManager.check` 的 scope/action 只描述"谁对哪个范围做什么"；
    本上下文补充"操作的资源是什么类型、属于哪类记忆"，供按 memory_type /
    pipeline profile 分流的权限策略使用。recall 先从规范化 FilterExpr 提取逻辑上
    强制的唯一等值，再由 extensions 的非空值覆盖；API 随后把授权路由值回注为系统
    谓词。已有 unit 的 memory_type 则必须由 API/Engine 从真源元数据解析。
    """

    # write_input / query / memory_list / memory_unit / delete_selector / admin / job
    resource_type: str = ""
    memory_type: str = ""  # coding / episodic / procedural ...
    pipeline: str = ""  # pipeline profile 名（如调用侧或真源元数据已知）
    unit_id: str = ""  # 已存在记忆单元 id；非 unit 操作为空
    scope: Scope = field(default_factory=Scope)  # 资源真实归属；未知时为空 scope
    tags: tuple[str, ...] = ()
    # 路由值恒为字符串：构造点（_write/_recall_permission_context）已做 str().strip()
    # 归一化，delegate 选择也按字符串比较。此处不随 MemoryUnit 的 metadata 值类型放宽。
    metadata: dict[str, str] = field(default_factory=dict)
    # 空间授权事实快照，由鉴权点一次读取后折算传入（F07「空间事实的两层投影与传入通道」）。
    # 取结构化字段而非塞进 metadata：后者是字符串映射，装不下成员元组与归属登记；序列化为
    # JSON 串的备选把判定依据的类型检查由编译期挪到运行期，而这是安全属性。
    # 该字段是空间事实进入判定的通道；换判定宿主时改传入通道即可，判据主体不动。
    space_facts: SpaceAuthorizationFacts | None = None


class Channel(str, Enum):
    """演进执行通道（架构 §8 双通道）。"""

    HOT = "hot"  # 在线：低时延的即时写入与轻量更新
    BACKGROUND = "background"  # 离线：异步做重的抽取/升华/重索引


class JobStatus(str, Enum):
    PENDING = "pending"  # 已提交待执行
    RUNNING = "running"  # 执行中
    SUCCEEDED = "succeeded"  # 成功完成
    FAILED = "failed"  # 执行失败
    CANCELLED = "cancelled"  # 已取消


@dataclass
class JobInfo:
    """一次调度任务的状态。"""

    id: str = ""  # 任务 id（submit 返回）
    channel: Channel = Channel.BACKGROUND  # 执行通道
    mode: str = ""  # 演进阶段（EvolveMode 的值）
    scope: Scope = field(default_factory=Scope)  # 演进作用范围
    status: JobStatus = JobStatus.PENDING  # 当前状态
    detail: dict[str, str] = field(default_factory=dict)  # 附加信息（进度/错误原因等）


class UpdateMode(str, Enum):
    """``update`` 的版本语义：决定修正后是否保留旧内容、用同 id 还是新 id。"""

    # 默认、非破坏式：新建一条新 id 的记忆承载修正后内容，旧记忆标记 superseded，
    # 新记忆 supersedes 指向旧 id。
    SUPERSEDE = "supersede"
    # 原地改写、沿用同 id，旧内容不单独留存为记忆（仅审计留痕）。
    OVERWRITE = "overwrite"


@dataclass
class MemoryPatch:
    """``update`` 的修正内容：仅非 ``None`` 字段生效。

    ``mode`` 决定版本语义（架构 §3.1）：默认 ``SUPERSEDE`` 非破坏式——生成
    新 id 的新版本、旧版标记 superseded、新版经 ``supersedes`` 记版本链；
    ``OVERWRITE`` 则原地覆写、沿用同 id，旧内容仅留审计。
    """

    content: str | None = None  # 修正后的内容投影
    tier: MemoryTier | None = None  # 重新归类认知角色
    tags: list[str] | None = None  # 整体替换标签
    system_metadata: dict[str, MetadataValueType] | None = None  # 合并更新系统元数据
    user_metadata: dict[str, MetadataValueType] | None = None  # 合并更新用户元数据
    t_valid: datetime | None = None  # 调整生效时间（双时间模型）
    t_invalid: datetime | None = None  # 调整失效时间
    mode: UpdateMode = UpdateMode.SUPERSEDE  # 版本语义：保留原有(新 id) / 覆盖(同 id)


class DeleteMode(str, Enum):
    """``delete`` 的语义：遗忘/降权/归档为非破坏式（架构 §9），purge 为合规硬删除。"""

    FORGET = "forget"  # 遗忘：标记 forgotten（可审计、按策略可恢复）
    ARCHIVE = "archive"  # 归档：转冷，不参与默认召回
    DOWNWEIGHT = "downweight"  # 降权：保持 active，仅降低重要度
    PURGE = "purge"  # 完全删除：物理删除真源与全部派生索引（合规删除，不可恢复，仅留审计记录）


@dataclass(frozen=True)
class WriteTargets:
    """一次写入的候选空间集合与兜底落点（F07「多空间读写」）。

    ``fallback`` 为 ``None`` 表示兜底落点不在候选集内，即判不准时无处可落。此时整次写入
    应被拒绝，但**拒绝由鉴权点抛出，不在控制层**：控制层只出集合运算结果，权限异常的语义
    留在 PEP。静默落到别处等于把兜底落点交给判定实现决定。
    """

    candidates: tuple[Scope, ...] = ()
    fallback: Scope | None = None


@dataclass
class DeleteSelector:
    """``delete`` 的目标选择：各条件取「与」，至少给出一项。"""

    unit_ids: list[str] = field(default_factory=list)  # 按 id 指定
    scope: Scope | None = None  # 限定归属 scope
    tags: list[str] = field(default_factory=list)  # 命中任一标签
    before: datetime | None = None  # 仅命中 t_message 早于此时间的
    # 结构化谓词。删除连带按判定标签或作者主体标记逐条清除，这两项都在 metadata 上，
    # tags 表达不了（tags 是标签数组，判定标签是键值对）。
    filters: FilterExpr | None = None
    mode: DeleteMode = DeleteMode.FORGET  # 删除语义
