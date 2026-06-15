"""MemoryUnit — 记忆单元，最小记忆载体（架构 §3.1）及其枚举。

数据层（真源）中存储的原子记录：无论文档还是结构化形态，逻辑模型一致。
接入层产出它，构建层落盘并在其上建索引，检索层与控制层读取它，
因此放在 ``common.type_def``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .scope import Scope


class Modality(str, Enum):
    """来源模态：多模态在接入层规约为 content 文本投影 + assets 原模态资产。"""

    TEXT = "text"  # 对话/纯文本
    IMAGE = "image"  # 图像
    AUDIO = "audio"  # 音频
    VIDEO = "video"  # 视频
    CODE = "code"  # 代码
    DOCUMENT = "document"  # 文档（PDF/Office 等）


class MemoryTier(str, Enum):
    """认知角色（记忆分类维度之一），决定常驻上下文 or 按需检索。"""

    WORKING = "working"  # 工作记忆：当前会话/任务态，易失
    CORE = "core"  # 核心记忆：高价值画像类，常驻上下文
    EPISODIC = "episodic"  # 情景记忆：发生过什么（事件/经历）
    SEMANTIC = "semantic"  # 语义记忆：知道什么（事实/概念/偏好）
    PROCEDURAL = "procedural"  # 程序性记忆：怎么做（技能/流程/模式）
    ARCHIVAL = "archival"  # 归档记忆：低频长尾，按需深挖


class LifecycleState(str, Enum):
    """生命周期：标记失效而非物理删除（非破坏式更新）。"""

    ACTIVE = "active"  # 有效（默认可被召回）
    SUPERSEDED = "superseded"  # 被取代：有新版本接替（版本更替的旧版）
    ARCHIVED = "archived"  # 归档：仍有效但转冷，不参与默认召回
    FORGOTTEN = "forgotten"  # 被遗忘：过期/低价值/合规要求，无继任者


@dataclass
class Temporal:
    """双时间模型：发生/摄入/有效期，支持时间点回溯（as_of）。"""

    t_event: datetime | None = None  # 事件时间：现实中发生/成立的时间
    t_ingest: datetime | None = None  # 摄入时间：系统记下它的时间
    t_valid: datetime | None = None  # 生效时间：系统从何时起认为它为真
    t_invalid: datetime | None = None  # 失效时间：不再认为为真；None 表示仍有效


@dataclass
class MemoryUnit:
    """一条记忆：内容投影 + 原模态资产引用 + 归属/时间/血缘/生命周期。"""

    id: str = ""  # 全局唯一 id（每条记忆/每个版本各一）
    scope: Scope = field(default_factory=Scope)  # 归属（隔离与共享的依据）
    tier: MemoryTier = MemoryTier.EPISODIC  # 认知角色分类
    content: str = ""  # 内容：可治理的文本/结构投影（索引与检索的对象）
    assets: list[str] = field(default_factory=list)  # 原模态资产引用（图像/音频原件等）
    source: Modality = Modality.TEXT  # 来源模态
    source_ref: str = ""  # 来源引用（RawPayload id / 会话 id 等，可溯源）
    temporal: Temporal = field(default_factory=Temporal)  # 双时间
    provenance: list[str] = field(default_factory=list)  # 演进血缘（多→一合成）：由哪些 unit 提取/升华/合并而来；来源 unit 可仍有效
    supersedes: str = ""  # 版本链（一→一更替）：本版本取代的上一版 id（update 的 SUPERSEDE 模式产生）；空表示首版
    tags: list[str] = field(default_factory=list)  # 标签（检索前置过滤用）
    metadata: dict[str, str] = field(default_factory=dict)  # 其他元数据（置信度/重要度等）
    lifecycle: LifecycleState = LifecycleState.ACTIVE  # 生命周期状态
