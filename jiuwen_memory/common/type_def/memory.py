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

MetadataValueType = str | int | float | bool | None | list[str]
_NON_PROPAGATED_SYSTEM_METADATA_KEYS = frozenset({"infer", "procedural", "middle"})


def inherited_system_metadata(units: list[MemoryUnit]) -> dict[str, MetadataValueType]:
    """返回派生记忆可安全继承的系统元数据交集。"""
    if not units:
        return {}
    common = {
        key: value
        for key, value in units[0].system_metadata.items()
        if key not in _NON_PROPAGATED_SYSTEM_METADATA_KEYS
    }
    for unit in units[1:]:
        common = {
            key: value
            for key, value in common.items()
            if key in unit.system_metadata and unit.system_metadata[key] == value
        }
    return common


def inherited_user_metadata(units: list[MemoryUnit]) -> dict[str, MetadataValueType]:
    """派生记忆的用户元数据：单源全量复制，多源只保留相等交集。"""
    if not units:
        return {}
    common = dict(units[0].user_metadata)
    for unit in units[1:]:
        common = {
            key: value
            for key, value in common.items()
            if key in unit.user_metadata and unit.user_metadata[key] == value
        }
    return common


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


class DedupDecision(str, Enum):
    """去重决策：Evolver 对候选 unit 与已有记忆的合并判定。

    - ADD: 新事实——与已有记忆不重叠，直接落盘
    - UPDATE: 更新——对已有记忆补充信息，合成新旧 content
    - SUPERSEDE: 取代——新版完全替代旧版，旧版标记 SUPERSEDED
    - NOOP: 无操作——与已有记忆完全重叠，跳过
    """

    ADD = "add"
    UPDATE = "update"
    SUPERSEDE = "supersede"
    NOOP = "noop"


@dataclass
class Temporal:
    """双时间模型：消息/事件/摄入/有效期，支持时间点回溯（as_of）。"""

    t_event: datetime | None = None  # 事件时间：内容描述的事件发生时间
    t_ingest: datetime | None = None  # 摄入时间：系统记录时间
    t_valid: datetime | None = None  # 生效时间：系统从何时起认为它为真
    t_invalid: datetime | None = None  # 失效时间：不再认为为真；None 表示仍有效
    t_message: datetime | None = None  # 消息时间：消息/对话发生时间


@dataclass
class Segment:
    """一段内容：可治理投影 + 其原模态资产 + 来源模态（三者一一对应，故同住一段）。

    一条记忆可由多段构成——如合成记忆聚合多源内容、多模态记忆含图/文各一段。
    ``content`` 是索引与检索的对象，``assets`` 是该段对应的原模态资产引用，
    ``source`` 是该段的来源模态。
    """

    content: str = ""  # 内容：可治理的文本/结构投影（索引与检索的对象）
    assets: list[str] = field(default_factory=list)  # 本段原模态资产引用（图像/音频原件等）
    source: Modality = Modality.TEXT  # 本段来源模态


@dataclass
class ContentLayers:
    """architecture §9.1 分层披露标注的承载结构。"""

    l0: str = ""  # 概要，50-100 字，供紧预算注入/快速浏览
    l1: str = ""  # 片段/要点，200-500 字，供上下文增强


@dataclass
class MemoryUnit:
    """一条记忆：多段内容投影 + 归属/时间/血缘/生命周期。

    ``scope`` 是 unit 级单一归属（隔离与共享的依据、存储命名空间键）；内容侧
    拆成 ``segments`` 列表，支持一条记忆聚合多段内容/资产/模态。
    ``content``/``assets``/``source`` 是把多段折叠成单一表示的**只读视图属性**，
    供「只要一段文本/全部资产/主模态」的消费者沿用，不改变 ``segments`` 这一
    存储真相（单段时折叠结果即该段原值）。
    """

    id: str = ""  # scope 内唯一 id（每条记忆/每个版本各一）
    scope: Scope = field(default_factory=Scope)  # 归属（隔离与共享的依据，unit 级单一 owner）
    tier: MemoryTier = MemoryTier.EPISODIC  # 认知角色分类
    layers: ContentLayers = field(default_factory=ContentLayers)
    # 多段内容投影（每段含 content+assets+source）
    segments: list[Segment] = field(default_factory=list)
    source_ref: str = ""  # 来源引用（RawPayload id / 会话 id 等，可溯源）
    temporal: Temporal = field(default_factory=Temporal)  # 双时间
    # 演进血缘（多→一合成）：由哪些 unit 提取/升华/合并而来；来源 unit 可仍有效
    provenance: list[str] = field(default_factory=list)
    # 版本链（一→一更替）：本版本取代的上一版 id；空表示首版
    supersedes: str = ""
    tags: list[str] = field(default_factory=list)  # 标签（检索前置过滤用）
    # 引擎可解释的系统控制与内部状态。用户自定义字段不得写入此命名空间。
    system_metadata: dict[str, MetadataValueType] = field(default_factory=dict)
    # 用户自定义元数据：内核只存储、索引、过滤和返回，不解释其业务含义。
    user_metadata: dict[str, MetadataValueType] = field(default_factory=dict)
    lifecycle: LifecycleState = LifecycleState.ACTIVE  # 生命周期状态
    # L2 记忆里由大模型抽取得到的实体文本（明文）。entity linker 建反向索引时
    # 只消费本字段构造 EntityMention（display_name=实体文本，normalized_name 走
    # normalizer 归一化）；为空时该 unit 不入实体索引（已砍 spaCy 兜底，实体完全
    # 由 LLM 在写入前抽取填充此字段）。
    entities: list[str] = field(default_factory=list)

    @property
    def content(self) -> str:
        """所有段内容的合并视图（只读）：以换行连接；单段即该段内容。"""
        return "\n".join(s.content for s in self.segments)

    @property
    def assets(self) -> list[str]:
        """所有段原模态资产引用的扁平合并（只读）。"""
        return [a for s in self.segments for a in s.assets]

    @property
    def source(self) -> Modality:
        """主来源模态（只读）：首段模态；无段时按 TEXT。"""
        return self.segments[0].source if self.segments else Modality.TEXT


# -- KV key 前缀（建索引记忆） ----------------------------------------------- #
# 真源 KV key 按「是否建索引」带前缀（见 F02 决策6）：建索引记忆 /memory/{id}、
# 未建索引的 infer 原文 /messages/{id}（MESSAGES_KEY_PREFIX 见 raw.py）。
# 所有落盘/回查建索引记忆的点用 memory_key，保证同前缀读写对齐。

MEMORY_KEY_PREFIX = "/memory/"

# 开放有效期（``Temporal.t_invalid is None``，即"仍有效"）在索引里的哨兵值：
# 9999-12-31T23:59:59Z 的 epoch 毫秒。
#
# 真源的 None 无法参与 ``t_invalid > as_of`` 下推——字段不写入索引，真后端按缺失
# 字段排他，恰好滤掉回溯查询最该命中的那批活跃记忆。落哨兵后该谓词对开放区间成立。
# 哨兵只存在于索引投影，真源仍是 None，``UnitReader.valid_at`` 的判定不受影响。
#
# 取值须小于 2^53：ES 的 metadata 数值字段由 dynamic_template 映射为 double，
# 超出该范围的整数会丢精度，相邻时间戳将无法区分。
T_INVALID_OPEN = 253402300799000

# 未知事件时间（``Temporal.t_event is None``）在索引里的哨兵值：``0``。
#
# F07 净化后派生 unit 的 ``t_event`` 常为 None（仅内容提取不到事件时间）。真源 None
# 让事件窗下推 ``t_event GTE / LT`` 在索引里按缺失字段排他——含时间词 query 对这批
# unit 系统性空召回。落哨兵后，``build_system_filters`` 把事件窗构造为
# ``OR(AND(GTE from, LT to), EQ 0)``，未知时间 unit 不再被窗下推清空。
#
# 哨兵存在于索引投影与后置复核（``memory_filter._field_value``），真源仍是 None；
# ``in_event_window`` 仍读真源 datetime、对 None/naive 放行，不受影响。
# 取 ``0``：早于任何真实 epoch（1970-01-01 起），与真实事件时间碰撞极罕见；
# 改哨兵值须同步改索引投影、``build_system_filters`` 谓词、``memory_filter`` 三处。
T_EVENT_UNKNOWN = 0


def memory_key(unit_id: str) -> str:
    """建索引记忆的 KV key。"""
    return f"{MEMORY_KEY_PREFIX}{unit_id}"
