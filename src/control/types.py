"""控制层接口涉及的数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from common.type_def import MemoryTier, Scope


class Action(str, Enum):
    """权限动作。"""

    READ = "read"  # 读取/检索
    WRITE = "write"  # 写入新记忆
    UPDATE = "update"  # 修正已有记忆
    DELETE = "delete"  # 遗忘/降权/归档
    SHARE = "share"  # 再授权给其他 scope


@dataclass
class Grant:
    """一条跨 scope 授权：grantor 把自己 scope 内的某些动作授权给 grantee。"""

    grantor: Scope = field(default_factory=Scope)  # 授权方 scope
    grantee: Scope = field(default_factory=Scope)  # 被授权方 scope
    actions: list[Action] = field(default_factory=list)  # 授权的动作集合
    expires_at: datetime | None = None  # 授权过期时间；None 表示长期有效


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

    SUPERSEDE = "supersede"  # 保留原有（默认、非破坏式）：新建一条新 id 的记忆承载修正后内容，旧记忆标记 superseded 保留，新记忆 supersedes 指向旧 id
    OVERWRITE = "overwrite"  # 覆盖：原地改写、沿用同 id，旧内容不单独留存为记忆（仅审计留痕）；用于纠错等无需版本史的场景


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
    metadata: dict[str, str] | None = None  # 合并更新元数据
    t_valid: datetime | None = None  # 调整生效时间（双时间模型）
    t_invalid: datetime | None = None  # 调整失效时间
    mode: UpdateMode = UpdateMode.SUPERSEDE  # 版本语义：保留原有(新 id) / 覆盖(同 id)


class DeleteMode(str, Enum):
    """``delete`` 的语义：遗忘/降权/归档为非破坏式（架构 §9），purge 为合规硬删除。"""

    FORGET = "forget"  # 遗忘：标记 forgotten（可审计、按策略可恢复）
    ARCHIVE = "archive"  # 归档：转冷，不参与默认召回
    DOWNWEIGHT = "downweight"  # 降权：保持 active，仅降低重要度
    PURGE = "purge"  # 完全删除：物理删除真源与全部派生索引（合规删除，不可恢复，仅留审计记录）


@dataclass
class DeleteSelector:
    """``delete`` 的目标选择：各条件取「与」，至少给出一项。"""

    unit_ids: list[str] = field(default_factory=list)  # 按 id 指定
    scope: Scope | None = None  # 限定归属 scope
    tags: list[str] = field(default_factory=list)  # 命中任一标签
    before: datetime | None = None  # 仅命中 t_event 早于此时间的
    mode: DeleteMode = DeleteMode.FORGET  # 删除语义
